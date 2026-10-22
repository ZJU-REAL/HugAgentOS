"""Per-user integration credential storage paths."""
from pathlib import Path
from core.config.settings import settings

def yida_cache_dir(user_id: str) -> Path:
    """Backend-local openyida (Yida CLI) login-state persistence directory (one per user).

    Contains one subdirectory, bind-mounted into the sandbox's fixed Yida working directory:
    - ``workspace/`` → sandbox ``/home/ubuntu/yida-workspace`` (openyida's projectRoot:
      ``.cache/cookies-*.json`` login cookies + ``.cache/openyida-envs.json`` env config +
      project artifacts such as ``prd/`` and schema cache)

    Difference from dws/lark: openyida's login state does **not** respect $HOME; it lands
    under the "project root"'s ``.cache/`` (projectRoot = the cwd at command execution
    time, see the cwd fallback of findProjectRoot in openyida lib/core/utils.js). So the
    persistence scheme is not binding home subpaths but pinning each user a fixed
    in-sandbox working directory and binding it wholesale: the skill forces all openyida
    commands to run inside that directory, so cookies survive across sessions with the
    volume. Login is completed by QR scan inside the conversation
    (``openyida login --agent-qr``); the backend runs no openyida subprocess. The sandbox
    runs as ubuntu (UID 1000) and the backend is also 1000, so ownership matches.
    """
    return settings.storage.root / "yida_cache" / user_id


def yida_workspace_dir(user_id: str) -> Path:
    """Backend absolute path of this user's fixed Yida working directory (opensandbox
    bind source / cube inject-and-return source).

    The sandbox mount point is fixed at ``/home/ubuntu/yida-workspace``; the yida skill
    requires all openyida commands to ``cd`` into that directory, so the ``.cache/``
    login state and project artifacts all land on the persistent volume.
    """
    return yida_cache_dir(user_id) / "workspace"


def yida_shared_workspace_dir() -> Path:
    """Yida working directory for the script-runner shared sandbox (**one per deployment**,
    not per-user).

    script-runner is a shared container with no user concept (the provider ignores
    user_id; rootfs is read_only) and cannot do per-user credential isolation — compose
    mounts this directory at the container's ``/home/ubuntu/yida-workspace``, so the Yida
    login state is shared deployment-wide. Only suitable for single-user/dev environments;
    multi-user production should use opensandbox (per-user bind-mount) or cube (per-user
    inject + return). The backend pre-creates it with 0777 at startup (runner uid 1001 ≠
    backend uid 1000; cross-uid writes need wide-open permissions).
    """
    return settings.storage.root / "yida_cache" / "__shared__" / "workspace"


YIDA_WORKSPACE_MOUNT = "/home/ubuntu/yida-workspace"
