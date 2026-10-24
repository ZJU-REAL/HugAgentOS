"""Create provider volumes from authorized space grants."""
import os
import base64
from pathlib import Path
from fastapi import HTTPException
from core.files.spaces.access import fingerprint, resolve_grants
from core.files.storage.entries import validate_key

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
    if backend == "local":
        config["path"] = str(settings.storage.root.resolve())
        config["host_path"] = settings.sandbox.opensandbox_host_storage_path
    payload = {"grants": fingerprint(grants), "backend": backend, "config": config}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).digest()
    return base64.b32encode(digest).decode("ascii").rstrip("=").lower()


def provider_volume(grant):
    from opensandbox.models.sandboxes import Host, OSSFS, Volume
    from core.config.settings import settings

    backend = os.getenv("STORAGE_TYPE", "local").lower()
    name = "space-" + fingerprint((grant,))[:24]
    if backend == "local":
        root = settings.storage.root.resolve()
        path = root / grant.prefix
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise HTTPException(503, "Invalid local space root")
        host_root = settings.sandbox.opensandbox_host_storage_path
        if not host_root:
            raise HTTPException(503, "HOST_STORAGE_PATH is required for space volumes")
        return Volume(name=name, host=Host(path=str(Path(host_root) / grant.prefix)),
            mount_path=grant.mount_path, read_only=not grant.writable)
    if backend not in ("s3", "oss", "obs"):
        raise HTTPException(503, "Unsupported storage provider")
    prefix = backend.upper()
    endpoint = os.getenv(prefix + "_ENDPOINT", "")
    bucket = os.getenv(prefix + "_BUCKET", "")
    access = os.getenv(prefix + ("_ACCESS_KEY_ID" if backend == "oss" else "_ACCESS_KEY"), "")
    secret = os.getenv(prefix + ("_ACCESS_KEY_SECRET" if backend == "oss" else "_SECRET_KEY"), "")
    if not all((endpoint, bucket, access, secret)):
        raise HTTPException(503, "Object mount configuration is incomplete")
    key_prefix = os.getenv("OSS_KEY_PREFIX", "").strip("/") if backend == "oss" else ""
    path = "/".join(part for part in (key_prefix, grant.prefix.rstrip("/")) if part)
    validate_key(path)
    options = ["hugagent_driver=s3fs", "allow_other", "uid=1000", "gid=1000",
        "umask=0022", "stat_cache_expire=0", "attr_timeout=0", "entry_timeout=0",
        "negative_timeout=0"]
    if backend == "oss":
        options.append("sigv2")
    else:
        options.extend(["sigv4", "endpoint=" + os.getenv(prefix + "_REGION", "us-east-1")])
        if backend == "s3":
            options.append("use_path_request_style")
    if not grant.writable:
        options.append("ro")
    return Volume(name=name, mount_path=grant.mount_path, read_only=not grant.writable,
        sub_path=path, ossfs=OSSFS(bucket=bucket, endpoint=endpoint, version="1.0",
            access_key_id=access, access_key_secret=secret, options=options))

def prepare_volumes(actor):
    from core.db.engine import SessionLocal
    with SessionLocal() as db:
        grants = resolve_grants(db, actor)
        return [provider_volume(grant) for grant in grants], {MOUNT_REVISION_TAG: mount_revision(grants)}


def provision_volumes(actor):
    """Initialize directory roots once, at sandbox creation."""
    from core.files.storage import get_storage
    volumes, metadata = prepare_volumes(actor)
    # Grants are represented by the authorized volume specifications. Resolve
    # writable prefixes once for initialization, then verify the same revision.
    from core.db.engine import SessionLocal
    with SessionLocal() as db:
        grants = resolve_grants(db, actor)
        if mount_revision(grants) != metadata[MOUNT_REVISION_TAG]:
            raise HTTPException(409, "Space permissions changed before provisioning")
        for grant in grants:
            if grant.writable or os.getenv("STORAGE_TYPE", "local").lower() == "local":
                get_storage().make_directory(grant.prefix)
    return volumes, metadata
