"""Community edition has no team workspace."""

from fastapi import HTTPException


def transfer_project(*args, **kwargs):
    raise HTTPException(403, "团队项目仅商业版 EE 可用")
