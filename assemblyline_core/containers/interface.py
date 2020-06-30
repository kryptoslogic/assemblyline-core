from typing import Tuple, List

from assemblyline.odm.models.service import DockerConfig


class ContainerError(RuntimeError):
    def __init__(self, message, service_name):
        super().__init__(message)
        self.service_name = service_name


class ContainerInterface:

    def restart(self):
        raise NotImplementedError()

    def stop(self):
        raise NotImplementedError()


class ContainerSetInterface:

    def restart(self):
        raise NotImplementedError()

    def stop(self):
        raise NotImplementedError()

    def scale(self, instances: int):
        raise NotImplementedError()


class ControllerInterface:

    def cpu_info(self) -> Tuple[float, float]:
        """Get the free and total CPU."""
        raise NotImplementedError()

    def memory_info(self) -> Tuple[float, float]:
        """Get the free and total RAM."""
        raise NotImplementedError()

    def start_container(self, name, config: DockerConfig) -> ContainerInterface:
        """Start a single container."""
        raise NotImplementedError()

    def get_container(self, name: str) -> ContainerInterface:
        """Get information about an individual container."""
        raise NotImplementedError()

    def find_containers(self, labels) -> List[ContainerInterface]:
        """Get information about a set of containers."""
        raise NotImplementedError()

    def start_set(self, name: str, config: DockerConfig, scale: int) -> ContainerSetInterface:
        """Start a set of identical containers."""
        raise NotImplementedError()

    def get_set(self, name) -> ContainerSetInterface:
        """"""
        raise NotImplementedError()

    # def find_sets(self, labels) -> List[ContainerSetInterface]:
    #     """"""
    #     raise NotImplementedError()
