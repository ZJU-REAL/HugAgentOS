"""Create provider volumes from authorized space grants."""
import os
import base64
from core.files.spaces.access import fingerprint, resolve_grants

MOUNT_REVISION_TAG = "hugagent-space-revision"

def mount_revision(grants):
    import hashlib
    import json
    from core.config.settings import settings
    backend = os.getenv("STORAGE_TYPE", "local").lower()
    prefix = backend.upper()
    config = {name: os.getenv(name, "") for name in (
        prefix + "_ENDPOINT", prefix + "_BUCKET", prefix + "_REGION",
        prefix + "_ACCESS_KEY", prefix + "_SECRET_KEY",
        prefix + "_ACCESS_KEY_ID", prefix + "_ACCESS_KEY_SECRET",
        "OSS_KEY_PREFIX",
    )}
    if os.getenv("DEPLOY_PROFILE") == "kubernetes":
        config["mount_mode"] = os.getenv("HUGAGENT_SPACE_MOUNT_MODE", "csi")
        config["claim"] = os.getenv("HUGAGENT_SPACE_CLAIM", "")
    if backend == "local":
        config["path"] = str(settings.storage.root.resolve())
        config["host_path"] = settings.sandbox.opensandbox_host_storage_path
    payload = {"grants": fingerprint(grants), "backend": backend, "config": config}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).digest()
    return base64.b32encode(digest).decode("ascii").rstrip("=").lower()


def provider_volume(grant):
    """The provider volume for one grant, or None when the transport copies files."""
    from core.files.spaces.transport import get_space_transport
    return get_space_transport().volume(grant)

def volumes_for(grants):
    return [v for v in map(provider_volume, grants) if v is not None]


def authorized_grants(actor, *, ordered):
    """The account's grants and their mount revision.

    A permission change closes the account's sandboxes and commits its grants while
    holding the account lock. A sandbox the controller already listed is closed by
    it, so a reused sandbox needs no ordering. A sandbox that may have been created
    during the change was missed by that listing; ``ordered=True`` reads under the
    lock, so the read either precedes the change (which then closes the sandbox) or
    sees its committed grants.
    """
    from contextlib import nullcontext
    from core.db.engine import SessionLocal
    from core.files.spaces.leases import space_lock
    with space_lock(actor) if ordered else nullcontext(), SessionLocal() as db:
        grants = resolve_grants(db, actor)
    return grants, mount_revision(grants)


def provision_volumes(actor):
    """Volumes for a new sandbox, tagged with the revision they were built from.

    Runs without the account lock: the sandbox's first use checks it against
    ``authorized_grants(actor, ordered=True)``.
    """
    from core.db.engine import SessionLocal
    from core.files.storage import get_storage
    with SessionLocal() as db:
        grants = resolve_grants(db, actor)
    volumes = volumes_for(grants)
    local = os.getenv("STORAGE_TYPE", "local").lower() == "local"
    for grant in grants:
        if grant.writable or local:
            get_storage().make_directory(grant.prefix)
    return volumes, {MOUNT_REVISION_TAG: mount_revision(grants)}
