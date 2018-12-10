import time
import json

from assemblyline.remote.datatypes.queues.named import NamedQueue
from dispatch_hash import DispatchHash
from assemblyline.datastore import odm
from configuration import ConfigManager
from models import build_result_key
import watcher


def service_queue_name(service):
    return 'service-queue-'+service


@odm.model()
class FileTask(odm.Model):
    sid = odm.Keyword()
    file_hash = odm.Keyword()
    file_type = odm.Keyword()
    depth = odm.Integer()


@odm.model()
class ServiceTask(odm.Model):
    sid = odm.Keyword()
    file_hash = odm.Keyword()
    file_type = odm.Keyword()
    depth = odm.Integer()
    service_name = odm.Keyword()
    service_config = odm.Keyword()


DISPATCH_QUEUE = 'dispatch-file'
SUBMISSION_QUEUE = 'submission'


class FileDispatcher:

    def __init__(self, datastore, redis):
        self.ds = datastore
        self.submissions = datastore.submissions
        self.results = datastore.results
        self.errors = datastore.errors
        self.config = ConfigManager(datastore)
        self.redis = redis
        self.running = True
        self.submission_queue = NamedQueue(SUBMISSION_QUEUE, *redis)
        self.file_dispatch = NamedQueue(SUBMISSION_QUEUE, *redis)
        self.timeout_seconds = 30 * 60

    def handle(self, message):
        """ Handle a message describing a file to be processed.

        This file may be:
            - A new submission or extracted file.
            - A file that has just completed a stage of processing.
            - A file that has not completed a a stage of processing, but this
              call has been triggered by a timeout or similar.

        If the file is totally new, we will setup a dispatch table, and fill it in.

        Once we make/load a dispatch table, we will dispatch whichever group the table
        shows us hasn't been completed yet.

        When we dispatch to a service, we check if the task is already in the dispatch
        queue. If it isn't proceed normally. If it is, check that the service is still online.
        """
        # Read the message content
        task = FileTask(json.loads(message))
        file_hash = task.file_hash
        submission = self.submissions.get(task.sid)

        # Refresh the watch on the submission, we are still working on it
        watcher.touch(self.redis, key=task.sid, timeout=self.timeout_seconds, queue=SUBMISSION_QUEUE, message={'sid': task.sid})

        # Open up the file/service table for this submission
        process_table = DispatchHash(task.sid, *self.redis)

        # Calculate the schedule for the file
        schedule = self.config.build_schedule(submission, task.file_type)

        # Go through each round of the schedule removing complete/failed services
        # Break when we find a stage that still needs processing
        outstanding = {}
        tasks_remaining = 0
        while schedule and not outstanding:
            stage = schedule.pop(0)

            for service in stage:
                # If the result is in the process table we are fine
                if process_table.finished(file_hash, service):
                    continue

                # Check if something, an error/a result already exists, to resolve this service
                config = self.config.build_service_config(service, submission)
                access_key = self.find_results(submission, file_hash, service, config)
                if access_key:
                    tasks_remaining = process_table.finish(file_hash, service, access_key)
                    continue

                outstanding[service] = config

        # Try to retry/dispatch any outstanding services
        if outstanding:
            for service, config in outstanding.items():
                # Check if this submission has already dispatched this service, and hasn't timed out yet
                if time.time() - process_table.dispatch_time(file_hash, service) < self.config.service_timeout(service):
                    continue

                # Build the actual service dispatch message
                service_task = ServiceTask(dict(
                    service_name=service,
                    service_config=json.dumps(config),
                    **task.as_primitives()
                ))

                queue = NamedQueue(service_queue_name(service), *self.redis)
                queue.push(service_task.as_primitives())
                process_table.dispatch(file_hash, service)

        else:
            # There are no outstanding services, this file is done

            # If there are no outstanding ANYTHING for this submission,
            # send a message to the submission dispatcher to finalize
            if process_table.all_finished() and tasks_remaining == 0:
                self.submission_queue.push({'sid': submission.id})

    def find_results(self, sid, file_hash, service, config):
        """
        Try to find any results or terminal errors that satisfy the
        request to run `service` on `file_hash` for the configuration in `submission`
        """
        # Look for results that match this submission/hash/service config
        key = build_result_key(file_hash, service, config)
        if self.results.exists(key):
            return key

        # NOTE these searches can be parallel
        # NOTE these searches need to be changed to match whatever the error log is set to
        # Search the errors for one matching this submission/hash/service
        # that also has a terminal flag
        results = self.errors.search(f"sid:{sid} AND file_hash:{file_hash} AND service:{service} "
                                     "AND catagory:'terminal'", rows=1, fl=[self.ds.ID])
        for result in results['items']:
            return result.id

        # Count the crash or timeout errors for submission/hash/service
        results = self.errors.search(f"sid:{sid} AND file_hash:{file_hash} AND service:{service} "
                                     "AND (catagory:'timeout' OR catagory:'crash')", rows=0)
        if results['total'] > self.config.service_failure_limit(service):
            return 'errors'

        # No reasons not to continue processing this file
        return False
