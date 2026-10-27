"""Docker inventory is a moving snapshot, for every reader in the controller.

docker-py's ``containers.list()`` lists ids, then inspects each one. A container
removed in between (an expiration timer, a delete request, backend reclamation)
raises ``NotFound`` and fails the whole listing unless ``ignore_removed=True``.
Creation, listing, startup recovery and failure cleanup all enumerate while
deletions run, so the controller makes removal tolerance the process-wide
default instead of trusting each upstream call site to pass the flag.
"""

import functools

_MARKER = "_hugagent_tolerates_removal"


def install():
    from docker.models.containers import ContainerCollection

    original = ContainerCollection.list
    if getattr(original, _MARKER, False):
        return

    @functools.wraps(original)
    def list_tolerating_removal(self, *args, **kwargs):
        kwargs.setdefault("ignore_removed", True)
        return original(self, *args, **kwargs)

    setattr(list_tolerating_removal, _MARKER, True)
    ContainerCollection.list = list_tolerating_removal
