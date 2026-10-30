"""Inject credential-isolated object mount helper into each sandbox Pod."""
import json
import os
import re

def apply_object_volumes(pod_spec, volumes):
    objects = [v for v in volumes if v.ossfs is not None]
    if not objects:
        return volumes
    names = ("OPENSANDBOX_SPACE_MOUNT_IMAGE", "HUGAGENT_SPACE_SECRET_NAME",
             "HUGAGENT_SPACE_ENDPOINT", "HUGAGENT_SPACE_BUCKET", "HUGAGENT_SPACE_PREFIX")
    config = {k: os.environ.get(k, "") for k in names}
    if not all(config.values()):
        raise ValueError("Controlled object mount configuration is incomplete")
    if pod_spec.get("shareProcessNamespace") or pod_spec.get("hostPID"):
        raise ValueError("Object mounts require private process namespaces")
    pod_spec["shareProcessNamespace"] = False
    pod_spec["automountServiceAccountToken"] = False
    specs, roots = [], set()
    for volume in objects:
        obj = volume.ossfs
        match = re.fullmatch(r"/workspace/(myspace|teamspace)/([A-Za-z0-9_-]{1,128})", volume.mount_path)
        prefix = volume.sub_path or ""
        if (not match or not prefix.startswith(config["HUGAGENT_SPACE_PREFIX"].rstrip("/") + "/spaces/")
                or any(p in ("", ".", "..") for p in prefix.split("/"))):
            raise ValueError("Invalid authorized space path")
        if (obj.bucket != config["HUGAGENT_SPACE_BUCKET"] or obj.endpoint != config["HUGAGENT_SPACE_ENDPOINT"]
                or obj.access_key_id != "server-managed" or obj.access_key_secret != "server-managed"
                or obj.options):
            raise ValueError("Object mounts require fixed server credentials and options")
        roots.add(match[1])
        specs.append({"path": volume.mount_path, "prefix": prefix, "read_only": volume.read_only})
    if len({s["path"] for s in specs}) != len(specs):
        raise ValueError("Duplicate object mount destination")
    mounts = pod_spec["containers"][0].setdefault("volumeMounts", [])
    if any(m["mountPath"].rstrip("/") in ("/workspace", "/workspace/myspace", "/workspace/teamspace") for m in mounts):
        raise ValueError("Space root conflicts with another volume")
    pod_volumes = pod_spec.setdefault("volumes", [])
    helper_mounts = []
    for root in sorted(roots):
        name, path = "hugagent-" + root, "/workspace/" + root
        if any(v["name"] == name for v in pod_volumes):
            raise ValueError("Reserved space volume name")
        pod_volumes.append({"name": name, "emptyDir": {}})
        mounts.append({"name": name, "mountPath": path, "mountPropagation": "HostToContainer"})
        helper_mounts.append({"name": name, "mountPath": path, "mountPropagation": "Bidirectional"})
    pod_volumes.append({"name": "hugagent-space-credentials", "secret": {
        "secretName": config["HUGAGENT_SPACE_SECRET_NAME"], "defaultMode": 256,
        "items": [{"key": "accessKeyID", "path": "access"}, {"key": "secretAccessKey", "path": "secret"}]}})
    helper_mounts.append({"name": "hugagent-space-credentials", "mountPath": "/run/space-credentials", "readOnly": True})
    pod_spec.setdefault("initContainers", []).append({
        "restartPolicy": "Always",
        "name": "space-mount", "image": config["OPENSANDBOX_SPACE_MOUNT_IMAGE"],
        "env": [{"name": "SPACE_MOUNTS", "value": json.dumps(specs)},
                {"name": "S3_ENDPOINT", "value": config["HUGAGENT_SPACE_ENDPOINT"]},
                {"name": "S3_BUCKET", "value": config["HUGAGENT_SPACE_BUCKET"]},
                {"name": "S3_REGION", "value": os.environ.get("HUGAGENT_SPACE_REGION", "us-east-1")}],
        "volumeMounts": helper_mounts,
        # Kubernetes requires privileged for Bidirectional propagation.
        "securityContext": {"privileged": True, "runAsUser": 0, "runAsGroup": 0},
        "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                      "limits": {"cpu": "1", "memory": "512Mi"}},
        "startupProbe": {"exec": {"command": ["python3", "/opt/space-mount.py", "check"]},
                         "periodSeconds": 2, "timeoutSeconds": 2, "failureThreshold": 90},
        "readinessProbe": {"exec": {"command": ["python3", "/opt/space-mount.py", "check"]},
                          "periodSeconds": 2, "timeoutSeconds": 2, "failureThreshold": 1}})
    return [v for v in volumes if v.ossfs is None]
