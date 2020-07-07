
from .interface import ControllerInterface


class DockerController(ControllerInterface):
    """A controller for *non* swarm mode docker."""

    def __init__(self, logger, prefix='', labels=None, cpu_overallocation=1, memory_overallocation=1):
        """
        :param logger: A logger to report status and debug information.
        :param prefix: A prefix used to distinguish containers launched by this controller.
        :param cpu_overallocation: A multiplier on CPU usage. (2 means act like there are twice as many CPU present)
        :param memory_overallocation: A multiplier on memory usage. (2 means act like there is twice as much memory)
        """
        # Connect to the host docker port
        import docker
        self.client = docker.from_env()
        self.log = logger
        self.global_mounts: List[Tuple[str, str]] = []
        self._prefix: str = prefix
        self._labels = labels

        for network in self.client.networks.list(names=['external']):
            self.external_network = network
            break
        else:
            self.external_network = self.client.networks.create(name='external', internal=False)
        self.networks = {}

        # CPU and memory reserved for the host
        self._reserved_cpu = 0.3
        self._reserved_mem = 500
        self.cpu_overallocation = cpu_overallocation
        self.memory_overallocation = memory_overallocation
        self._profiles = {}

        # Prefetch some info that shouldn't change while we are running
        self._info = self.client.info()

        # We aren't checking for swarm nodes
        assert not self._info['Swarm']['NodeID']
