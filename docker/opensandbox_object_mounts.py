"""S3-compatible object volumes for the trusted execution controller."""
import hashlib
import json
import posixpath
from uuid import uuid4

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

def resolve_paths(self, volume):
    root = self.app_config.storage.ossfs_mount_root
    if not root or not root.startswith("/"):
        raise ValueError("An absolute object mount root is required")
    key = mount_identity(volume)
    # A FUSE connection belongs to one sandbox volume. Docker Desktop retains
    # bind aliases after unmount; a new connection must get a new host path.
    generation = getattr(volume, "_hugagent_mount_generation", None)
    if generation is None:
        generation = uuid4().hex
        object.__setattr__(volume, "_hugagent_mount_generation", generation)
    path = posixpath.join(root, key + "-" + generation)
    return path, path

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

def build_command(self, volume, source, backend_path, endpoint_url, passwd_file):
    self._validate_endpoint_url(endpoint_url)
    self._validate_mount_path(backend_path)
    command = ["s3fs", source, backend_path, "-o", "url=" + endpoint_url,
        "-o", "passwd_file=" + passwd_file,
        # Keep a fixed 2 GiB reserve for temporary writes. The s3fs default
        # reserves 10% of the whole host disk and can reject healthy data volumes.
        "-o", "ensure_diskfree=2048"]
    for option in validated_options(self, volume):
        command.extend(["-o", option])
    return command

def install():
    from opensandbox_server.services.docker.ossfs_mixin import OSSFSMixin
    OSSFSMixin._resolve_ossfs_paths = resolve_paths
    OSSFSMixin._build_ossfs_v1_command = build_command
