"""
A process that manages tracking and running update commands for the AL services.

TODO:
    - docker build updates
    - If the service update interval changes in datastore move the next update time

"""
import os
import random
import sched
import shutil
import string
import tempfile
import time
from contextlib import contextmanager
from threading import Thread
from typing import Dict

import docker
import yaml
from assemblyline.remote.datatypes.lock import Lock
from kubernetes.client import V1Job, V1ObjectMeta, V1JobSpec, V1PodTemplateSpec, V1PodSpec, V1Volume, \
    V1PersistentVolumeClaimVolumeSource, V1VolumeMount, V1EnvVar, V1Container, V1ResourceRequirements
from kubernetes import client, config
from kubernetes.client.rest import ApiException

from passlib.hash import bcrypt

from assemblyline.common.isotime import now_as_iso
from assemblyline.common.security import get_random_password, get_password_hash
from assemblyline.datastore.helper import AssemblylineDatastore
from assemblyline.odm.models.service import DockerConfig
from assemblyline.odm.models.user import User
from assemblyline.odm.models.user_settings import UserSettings
from assemblyline.remote.datatypes.hash import Hash
from assemblyline_core.server_base import CoreBase, ServiceStage

SERVICE_SYNC_INTERVAL = 30  # How many seconds between checking for new services, or changes in service status
UPDATE_CHECK_INTERVAL = 60  # How many seconds per check for outstanding updates
UPDATE_STAGES = [ServiceStage.Update, ServiceStage.Running]

# How to identify the update volume as a whole, in a way that the underlying container system recognizes.
FILE_UPDATE_VOLUME = os.environ.get('FILE_UPDATE_VOLUME', None)
# Where to find the update directory inside this container.
FILE_UPDATE_DIRECTORY = os.environ.get('FILE_UPDATE_DIRECTORY', None)

# How many past updates to keep for file based updates
UPDATE_FOLDER_LIMIT = 5
NAMESPACE = os.getenv('NAMESPACE', None)
UI_SERVER = os.getenv('UI_SERVER', 'localhost:5000')


@contextmanager
def temporary_api_key(ds: AssemblylineDatastore, user_name: str, permissions=('R', 'W')):
    """Creates a context where a temporary API key is available."""
    with Lock(f'user-{user_name}', timeout=10):
        name = ''.join(random.choices(string.ascii_lowercase, k=20))
        random_pass = get_random_password(length=48)
        user = ds.user.get(user_name)
        user.apikeys[name] = {
            "password": bcrypt.encrypt(random_pass),
            "acl": permissions
        }
        ds.user.save(user_name, user)

    try:
        yield f"{name}:{random_pass}"
    finally:
        with Lock(f'user-{user_name}', timeout=10):
            user = ds.user.get(user_name)
            user.apikeys.pop(name)
            ds.user.save(user_name, user)


class DockerUpdateInterface:
    """Wrap docker interface for the commands used in the update process.

    Volumes used for file updating on the docker interface are simply a host directory that gets
    mounted at different depths and locations in each container.

    FILE_UPDATE_VOLUME gives us the path of the directory on the host, so that we can mount
    it properly on new containers we launch. FILE_UPDATE_DIRECTORY gives us the path
    that it is mounted at in the update manager container.
    """
    def __init__(self):
        self.client = docker.from_env()
        self._external_network = None

    @property
    def external_network(self):
        """Lazy load the external network information from docker.

        In testing environments it may not exists, and we may not have docker socket anyway.
        """
        if not self._external_network:
            for network in self.client.networks.list(names=['external']):
                self._external_network = network
                break
            else:
                self._external_network = self.client.networks.create(name='external', internal=False)
        return self._external_network

    def launch(self, name, docker_config: DockerConfig, mounts, env, network, blocking: bool = True):
        """Run a container to completion."""
        container = self.client.containers.run(
            image=docker_config.image,
            name='update_' + name,
            network=network,
            restart_policy={'Name': 'no'},
            command=docker_config.command,
            volumes={os.path.join(row['volume'], row['source_path']): {'bind': row['dest_path'], 'mode': 'rw'}
                     for row in mounts},
            environment=[f'{_e.name}={_e.value}' for _e in docker_config.environment] +
                        [f'{k}={v}' for k, v in env.items()],
            detach=not blocking,
        )
        if docker_config.allow_internet_access:
            self.external_network.connect(container)

    def restart(self, service_name):
        for container in self.client.containers.list(filters={'label': f'component={service_name}'}):
            container.kill()


class KubernetesUpdateInterface:
    def __init__(self, prefix, namespace, priority_class):
        # Try loading a kubernetes connection from either the fact that we are running
        # inside of a cluster,
        try:
            config.load_incluster_config()
        except config.config_exception.ConfigException:
            # Load the configuration once to initialize the defaults
            config.load_kube_config()

            # Now we can actually apply any changes we want to make
            cfg = client.configuration.Configuration()

            if 'HTTPS_PROXY' in os.environ:
                cfg.proxy = os.environ['HTTPS_PROXY']
                if not cfg.proxy.startswith("http"):
                    cfg.proxy = "https://" + cfg.proxy
                client.Configuration.set_default(cfg)

            # Load again with our settings set
            config.load_kube_config(client_configuration=cfg)

        self.prefix = prefix.lower()
        self.b1api = client.AppsV1beta1Api()
        self.api = client.CoreV1Api()
        self.batch_api = client.BatchV1Api()
        self.namespace = namespace
        self.priority_class = priority_class

    def launch(self, name, docker_config: DockerConfig, mounts, env, blocking: bool = True):
        name = (self.prefix + 'update-' + name.lower()).replace('_', '-')

        try:
            self.batch_api.delete_namespaced_job(name=name, namespace=self.namespace, propagation_policy='Background')
            while True:
                self.batch_api.read_namespaced_job(namespace=self.namespace, name=name)
                time.sleep(1)
        except ApiException:
            pass

        volumes = []
        volume_mounts = []

        for index, mnt in enumerate(mounts):
            volumes.append(V1Volume(
                name=f'mount-{index}',
                persistent_volume_claim=V1PersistentVolumeClaimVolumeSource(
                    claim_name=mnt['volume'],
                    read_only=False
                ),
            ))

            volume_mounts.append(V1VolumeMount(
                name=f'mount-{index}',
                mount_path=mnt['dest_path'],
                sub_path=mnt['source_path'],
                read_only=False,
            ))

        metadata = V1ObjectMeta(
            name=name,
            labels={
                'app': 'assemblyline',
                'section': 'core',
                'component': 'update-script'
            }
        )

        environment_variables = [V1EnvVar(name=_e.name, value=_e.value) for _e in docker_config.environment]
        environment_variables.extend([V1EnvVar(name=k, value=v) for k, v in env.items()])

        cores = docker_config.cpu_cores
        memory = docker_config.ram_mb

        container = V1Container(
            name=name,
            image=docker_config.image,
            command=docker_config.command,
            env=environment_variables,
            image_pull_policy='Always',
            volume_mounts=volume_mounts,
            resources=V1ResourceRequirements(
                limits={'cpu': cores, 'memory': f'{memory}Mi'},
                requests={'cpu': cores / 4, 'memory': f'{int(memory / 4)}Mi'},
            )
        )

        pod = V1PodSpec(
            volumes=volumes,
            restart_policy='Never',
            containers=[container],
            priority_class_name=self.priority_class,
        )

        job = V1Job(
            metadata=metadata,
            spec=V1JobSpec(
                backoff_limit=1,
                completions=1,
                template=V1PodTemplateSpec(
                    metadata=metadata,
                    spec=pod
                )
            )
        )

        status = self.batch_api.create_namespaced_job(namespace=self.namespace, body=job).status

        if blocking:
            while not (status.failed or status.succeeded):
                time.sleep(3)
                status = self.batch_api.read_namespaced_job(namespace=self.namespace, name=name).status

            self.batch_api.delete_namespaced_job(name=name, namespace=self.namespace, propagation_policy='Background')

    def restart(self, service_name):
        name = (self.prefix + service_name.lower()).replace('_', '-')
        scale = self.b1api.read_namespaced_deployment_scale(name=name, namespace=self.namespace)
        scale.spec.replicas = 0
        self.b1api.replace_namespaced_deployment_scale(name=name, namespace=self.namespace, body=scale)


class ServiceUpdater(CoreBase):
    def __init__(self, redis_persist=None, logger=None, datastore=None):
        super().__init__('assemblyline.service.updater', logger=logger, datastore=datastore,
                         redis_persist=redis_persist)

        if not FILE_UPDATE_DIRECTORY:
            raise RuntimeError("The updater process must be run within the orchestration environment, "
                               "the update volume must be mounted, and the path to the volume must be "
                               "set in the environment variable FILE_UPDATE_DIRECTORY. Setting "
                               "FILE_UPDATE_DIRECTORY directly may be done for testing.")

        # The directory where we want working temporary directories to be created.
        # Building our temporary directories in the persistent update volume may
        # have some performance down sides, but may help us run into fewer docker FS overlay
        # cleanup issues. Try to flush it out every time we start. This service should
        # be a singleton anyway.
        self.temporary_directory = os.path.join(FILE_UPDATE_DIRECTORY, '.tmp')
        shutil.rmtree(self.temporary_directory, ignore_errors=True)
        os.mkdir(self.temporary_directory)

        self.services = Hash('service-updates', self.redis_persist)
        self.running_updates: Dict[str, Thread] = {}

        # Prepare a single threaded scheduler
        self.scheduler = sched.scheduler()

        #
        if 'KUBERNETES_SERVICE_HOST' in os.environ and NAMESPACE:
            self.controller = KubernetesUpdateInterface(prefix='alsvc_', namespace=NAMESPACE,
                                                        priority_class='al-core-priority')
        else:
            self.controller = DockerUpdateInterface()

    def sync_services(self):
        """Download the service list and make sure our settings are up to date"""
        self.scheduler.enter(SERVICE_SYNC_INTERVAL, 0, self.sync_services)

        # Get all the service data
        for service in self.datastore.list_all_services(full=True):

            # Ensure that any disabled services are not being updated
            if not service.enabled and self.services.exists(service.name):
                self.log.info(f"Service updates disabled for {service.name}")
                self.services.pop(service.name)

            if not service.enabled:
                continue

            # Ensure that any enabled services with an update config are being updated
            stage = self.get_service_stage(service.name)
            if stage in UPDATE_STAGES and service.update_config and not self.services.exists(service.name):
                self.log.info(f"Service updates enabled for {service.name}")
                self.services.add(
                    service.name,
                    dict(
                        next_update=now_as_iso(),
                        previous_update=now_as_iso(-10**10),
                        sha256=None,
                    )
                )

            if stage == ServiceStage.Update:
                record = self.services.get(service.name)
                if (record and record.get('sha256', None) is not None) or not service.update_config:
                    self._service_stage_hash.set(service.name, ServiceStage.Running)

    def try_run(self):
        """Run the scheduler loop until told to stop."""
        # Do an initial call to the main methods, who will then be registered with the scheduler
        self.sync_services()
        self.update_services()

        # Run as long as we need to
        while self.running:
            delay = self.scheduler.run(False)
            time.sleep(min(delay, 0.1))

    def update_services(self):
        """Check if we need to update any services.

        Spin off a thread to actually perform any updates. Don't allow multiple threads per service.
        """
        self.scheduler.enter(UPDATE_CHECK_INTERVAL, 0, self.update_services)

        # Check for finished update threads
        self.running_updates = {name: thread for name, thread in self.running_updates.items() if thread.is_alive()}

        # Check if its time to try to update the service
        for service_name, data in self.services.items().items():
            if data['next_update'] <= now_as_iso() and service_name not in self.running_updates:
                self.log.info(f"Time to update {service_name}")
                self.running_updates[service_name] = Thread(
                    target=self.run_update,
                    kwargs=dict(service_name=service_name)
                )
                self.running_updates[service_name].start()

    def run_update(self, service_name):
        """Common setup and tear down for all update types."""
        # noinspection PyBroadException
        try:
            # Check for new update with service specified update method
            service = self.datastore.get_service_with_delta(service_name)
            update_method = service.update_config.method
            update_data = self.services.get(service_name)
            update_hash = None

            try:
                # Actually run the update method
                if update_method == 'run':
                    update_hash = self.do_file_update(
                        service=service,
                        previous_hash=update_data['sha256'],
                        previous_update=update_data['previous_update']
                    )
                elif update_method == 'build':
                    update_hash = self.do_build_update()

                # If we have performed an update, write that data
                if update_hash is not None and update_hash != update_data['sha256']:
                    update_data['sha256'] = update_hash
                    update_data['previous_update'] = now_as_iso()
                else:
                    update_hash = None

            finally:
                # Update the next service update check time
                update_data['next_update'] = now_as_iso(service.update_config.update_interval_seconds)
                self.services.set(service_name, update_data)

            if update_hash:
                self.log.info(f"New update applied for {service_name}. Restarting service.")
                self.controller.restart(service_name=service_name)

        except BaseException:
            self.log.exception("An error occurred while running an update for: " + service_name)

    def do_build_update(self):
        """Update a service by building a new container to run."""
        raise NotImplementedError()

    def do_file_update(self, service, previous_hash, previous_update):
        """Update a service by running a container to get new files."""
        temp_directory = tempfile.mkdtemp(dir=self.temporary_directory)
        input_directory = os.path.join(temp_directory, 'input_directory')
        output_directory = os.path.join(temp_directory, 'output_directory')
        service_dir = os.path.join(FILE_UPDATE_DIRECTORY, service.name)

        try:
            os.makedirs(input_directory)
            os.makedirs(output_directory)

            username = self.ensure_service_account()

            with temporary_api_key(self.datastore, username) as api_key:

                # Write out the parameters we want to pass to the update container
                with open(os.path.join(input_directory, 'config.yaml'), 'w') as fh:
                    yaml.safe_dump({
                        'previous_update': previous_update,
                        'previous_hash': previous_hash,
                        'sources': [x.as_primitives() for x in service.update_config.sources],
                        'api_user': username,
                        'api_key': api_key,
                        'ui_server': UI_SERVER
                    }, fh)

                # Run the update container
                self.controller.launch(
                    name=service.name,
                    docker_config=service.update_config.run_options,
                    mounts=[
                        {
                            'volume': FILE_UPDATE_VOLUME,
                            'source_path': os.path.relpath(temp_directory, start=FILE_UPDATE_DIRECTORY),
                            'dest_path': '/mount/'
                        },
                    ],
                    env={
                        'UPDATE_CONFIGURATION_PATH': '/mount/input_directory/config.yaml',
                        'UPDATE_OUTPUT_PATH': '/mount/output_directory/'
                    },
                    network=f'service-net-{service.name}',
                    blocking=True,
                )

                # Read out the results from the output container
                results_meta_file = os.path.join(output_directory, 'response.yaml')

                if not os.path.exists(results_meta_file) or not os.path.isfile(results_meta_file):
                    self.log.warning(f"Update produced no output for {service.name}")
                    return None

                with open(results_meta_file) as rf:
                    results_meta = yaml.safe_load(rf)
                update_hash = results_meta.get('hash', None)

                # Erase the results meta file
                os.unlink(results_meta_file)

                # FILE_UPDATE_DIRECTORY/{service_name} is the directory mounted to the service,
                # the service sees multiple directories in that directory, each with a timestamp
                destination_dir = os.path.join(service_dir, service.name + '_' + now_as_iso())
                shutil.move(output_directory, destination_dir)

                # Remove older update files, due to the naming scheme, older ones will sort first lexically
                existing_folders = []
                for folder_name in os.listdir(service_dir):
                    folder_path = os.path.join(service_dir, folder_name)
                    if os.path.isdir(folder_path) and folder_name.startswith(service.name):
                        existing_folders.append(folder_name)
                existing_folders.sort()

                self.log.info(f'There are {len(existing_folders)} update folders for {service.name} in cache.')
                if len(existing_folders) > UPDATE_FOLDER_LIMIT:
                    extra_count = len(existing_folders) - UPDATE_FOLDER_LIMIT
                    self.log.info(f'We will only keep {UPDATE_FOLDER_LIMIT} updates, deleting {extra_count}.')
                    for extra_folder in existing_folders[:extra_count]:
                        # noinspection PyBroadException
                        try:
                            shutil.rmtree(os.path.join(service_dir, extra_folder))
                        except:
                            self.log.exception('Failed to delete update folder')

                return update_hash
        finally:
            # If the working directory is still there for any reason erase it
            shutil.rmtree(temp_directory, ignore_errors=True)

    def ensure_service_account(self):
        """Check that the update service account exists, if it doesn't, create it."""
        uname = 'update_service_account'

        if self.datastore.user.get_if_exists(uname):
            return uname

        user_data = User({
            "agrees_with_tos": "NOW",
            "classification": "RESTRICTED",
            "name": "Update Account",
            "password": get_password_hash(''.join(random.choices(string.ascii_letters, k=20))),
            "uname": uname,
            "type": ["signature_importer"]
        })
        self.datastore.user.save(uname, user_data)
        self.datastore.user_settings.save(uname, UserSettings())
        return uname


if __name__ == '__main__':
    ServiceUpdater().serve_forever()
