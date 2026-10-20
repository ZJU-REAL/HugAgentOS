"""S3-compatible object volumes for the trusted execution controller."""
import hashlib
import json
from pathlib import PurePosixPath

MANIFEST = "hugagent-space-mounts"

def mount_identity(volume):
    prefix = volume.sub_path or ""
    if prefix.startswith("/") or any(p in ("", ".", "..") for p in prefix.split("/")):
        raise ValueError("An object prefix is required")
    identity = {
        "bucket": volume.ossfs.bucket, "endpoint": volume.ossfs.endpoint,
        "prefix": prefix, "read_only": volume.read_only,
        "options": volume.ossfs.options,
        "credential": hashlib.sha256(
            (volume.ossfs.access_key_id + ":" + volume.ossfs.access_key_secret).encode()
        ).hexdigest(),
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

def validated_options(service, volume):
    service._validate_bucket_name(volume.ossfs.bucket)
    service._validate_endpoint_url(volume.ossfs.endpoint)
    options = list(volume.ossfs.options or ())
    if options.count("hugagent_driver=s3fs") != 1:
        raise ValueError("The object volume driver must be s3fs")
    options.remove("hugagent_driver=s3fs")
    allowed = {"allow_other", "uid", "gid", "umask", "stat_cache_expire",
        "attr_timeout", "entry_timeout", "negative_timeout", "sigv2", "sigv4",
        "endpoint", "use_path_request_style", "ro"}
    for option in options:
        if option.split("=", 1)[0] not in allowed:
            raise ValueError("Unsupported object mount option")
        service._validate_ossfs_option(option)
    if ("ro" in options) != volume.read_only:
        raise ValueError("Object mount options must match the access permission")
    return options

def validate_volume(service, volume):
    if not volume.ossfs.access_key_id or not volume.ossfs.access_key_secret:
        raise ValueError("Object volume credentials are required")
    service._validate_bucket_name(volume.ossfs.bucket)
    service._validate_endpoint_url(volume.ossfs.endpoint)
    service._validate_mount_path(volume.mount_path)
    mount_identity(volume)
    validated_options(service, volume)
    target = PurePosixPath(volume.mount_path)
    if (not target.is_absolute() or str(target) != volume.mount_path
            or ".." in target.parts or str(target) == "/"):
        raise ValueError("A canonical absolute object mount target is required")


def mount_manifest(volumes):
    return [{"target": volume.mount_path, "identity": mount_identity(volume),
             "read_only": volume.read_only}
            for volume in (volumes or []) if volume.ossfs is not None]


def install():
    from opensandbox_server.services.docker.ossfs_mixin import OSSFSMixin
    # Object mounts have one namespace-based lifecycle. No host FUSE paths.
    OSSFSMixin._validate_ossfs_volume = validate_volume
