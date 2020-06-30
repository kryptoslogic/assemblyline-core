
from .interface import ControllerInterface


class KubernetesController(ControllerInterface):
    def __init__(self, logger, prefix, labels, namespace, priority):
        self.logger = logger
        self.namespace = namespace
        self.default_labels = labels
        self.default_priority_class = priority
