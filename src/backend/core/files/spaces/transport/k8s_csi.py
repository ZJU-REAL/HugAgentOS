"""Kubernetes: mount authorized S3 prefixes through a prebound, namespace-local CSI claim."""
import os
import re

from fastapi import HTTPException

from core.files.spaces.transport.k8s_mount import KubernetesMountTransport


class KubernetesCsiTransport(KubernetesMountTransport):
    name = "k8s-csi"

    def volume(self, grant):
        from opensandbox.models.sandboxes import PVC
        if os.getenv("STORAGE_TYPE", "").lower() != "s3":
            raise HTTPException(503, "Native space mounts require the configured S3 CSI storage")
        claim = os.environ.get("HUGAGENT_SPACE_CLAIM", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,62}", claim):
            raise HTTPException(503, "Configure the namespace-local space CSI claim")
        return self._volume(grant, pvc=PVC(claim_name=claim, create_if_not_exists=False),
                            sub_path=self._prepared_prefix(grant))
