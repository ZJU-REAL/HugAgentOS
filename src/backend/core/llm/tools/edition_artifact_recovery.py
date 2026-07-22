"""Community Edition has no organization artifact recovery."""

from typing import Any, Optional


def recover_organization_artifact(*, file_path: str, scope: Any) -> tuple[bool, Optional[bytes]]:
    return False, None


__all__ = ["recover_organization_artifact"]
