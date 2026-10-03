"""Fail-closed errors. Callers turn these into a non-zero exit."""


class Rocky2debError(Exception):
    """Base error. exit_code 1 is a fault in the inputs we could not classify."""

    exit_code = 1


class Refused(Rocky2debError):
    """The operation is rejected. Nothing was applied or stored."""

    exit_code = 3


class SpecUnemittable(Refused):
    """The Debian source has no known upstream build type, so no spec is written."""


class CycleError(Refused):
    def __init__(self, nodes: list[str]):
        self.nodes = nodes
        super().__init__("dependency cycle: " + ", ".join(nodes))
