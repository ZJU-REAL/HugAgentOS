"""Account credential paths and in-place cleanup for persistent mounts."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from core.config.settings import settings

logger = logging.getLogger(__name__)


def purge_credential_dir(root: Path) -> None:
    """Empty a per-user credential tree **in place**, deleting every file but
    keeping every directory inode alive.

    Replaces ``shutil.rmtree(root)`` for the ``*_cache/{user_id}`` trees.
    Subdirectories of these trees are bind-mounted into the user's live
    sandboxes (see [[lark_cache_dir]] / [[dws_cache_dir]] / [[email_cache_dir]]),
    and a bind mount is bound to the **inode**, not the path: unlinking a mount
    source leaves every already-running sandbox looking at an orphaned
    directory it will see as empty forever, while credentials written by a
    later re-login land on a freshly created inode that sandbox can never see.
    Observed in practice as "扫码登录成功了，会话里却还报 not configured / 应用未配置"
    — the open chat's sandbox kept the deleted inode.

    Emptying in place keeps every inode, so disconnect and re-login take effect
    immediately in sandboxes that are already running. Leftover empty
    directories are harmless: the volume builders pre-create exactly these
    paths anyway. This is the same "delete files, never the directory" rule the
    Yida flow already follows, which is why Yida never hit the bug.
    """
    if not root.exists():
        return
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            fp = Path(dirpath) / name
            try:
                fp.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("[sandbox] purge credential file failed %s: %s", fp, exc)
























# openyida (Yida CLI) login-state persistence volume: unlike the "$HOME subpath" pattern of
# dws/lark/email — openyida's cookie/env config lands under the "project root" .cache/
# (projectRoot falls back to cwd); it honors neither $HOME nor any global config directory.
# So each user gets a fixed working directory bind-mounted into the sandbox, and the yida
# skill forces all openyida commands to cd into that directory before running (see the
# _common.yida_cache_dir comment).
