"""Synchronize the current authorized skill view into Kubernetes sandboxes."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import io
from pathlib import Path
import shlex
import tarfile
import uuid

from core.sandbox.errors import SandboxError
from core.sandbox.providers.opensandbox.runtime import logger
from core.sandbox.workspace.identity import SANDBOX_RUN_UID, SANDBOX_RUN_GID

# Serializes staging per sandbox object inside one process.
_LOCK_ATTR = "_jx_skill_view_lock"


def _database_visible(skill_ids, user_id):
    """Read mutable enablement/ownership freshly; loader listings include disabled rows."""
    from core.db.engine import SessionLocal
    from core.db.models import AdminSkill

    allowed = set(skill_ids)
    with SessionLocal() as db:
        rows = db.query(AdminSkill.skill_id, AdminSkill.is_enabled, AdminSkill.owner_user_id,
                        AdminSkill.dep_status).filter(AdminSkill.skill_id.in_(allowed)).all()
        for row in rows:
            if not row.is_enabled or row.dep_status != "ready" or (row.owner_user_id and row.owner_user_id != user_id):
                allowed.discard(row.skill_id)
    return allowed


def _view_files(user_id: str | None) -> list[tuple[str, bytes, int]]:
    """Freeze and validate the whole current view, independent of shell syntax."""
    from core.harness.registry.run_capabilities import has_run_capabilities
    if has_run_capabilities():
        raise SandboxError("Kubernetes skill views require cloud sources; desktop capability publication is unsupported")
    from core.capabilities.skills.publication import prepare_skill_view, publication_lock
    from core.capabilities.skills.config import get_sandbox_skills_dir
    from core.llm.api_runtime.agent_skills import _skill_files
    from core.capabilities.catalog.api import get_catalog

    # Read the latest administrator choices without discarding the shared cache.
    catalog = {str(item["id"]): bool(item.get("enabled"))
               for item in get_catalog(include_runtime_details=False, fresh=True).get("skills", [])}
    with publication_lock():
        view = prepare_skill_view(user_id)
        root = view if view is not None else get_sandbox_skills_dir()
        selected = {path.name: str(path) for path in sorted(root.iterdir())
                    if path.is_dir() and catalog.get(path.name, True)}
        visible = _database_visible(selected, user_id)
        selected = {name: path for name, path in selected.items() if name in visible}
        # Reuse explicit API package validation, including symlink rejection and
        # the bounded file closure. Publication errors stop the new command.
        files = []
        for target, content in _skill_files(selected):
            relative = target.removeprefix("/workspace/skills/")
            skill_id, name = relative.split("/", 1)
            mode = 0o755 if (Path(selected[skill_id]) / name).stat().st_mode & 0o111 else 0o644
            files.append((relative, content, mode))
        return files


def _view_digest(user_id: str | None, files: list[tuple[str, bytes, int]]) -> str:
    digest = hashlib.sha256(str(user_id or "").encode() + b"\0")
    for relative, content, mode in files:
        digest.update(relative.encode() + b"\0" + str(mode).encode() + b"\0")
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def _view_archive(files: list[tuple[str, bytes, int]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for relative, content, mode in files:
            entry = tarfile.TarInfo(relative)
            entry.size = len(content)
            entry.mode = mode
            archive.addfile(entry, io.BytesIO(content))
    return buffer.getvalue()


# Runs as uid 1000. Preserve unchanged directories and files so concurrent
# commands do not lose their working directory merely because another command
# starts. Changed files are replaced atomically; revoked files are removed.
_SYNC_VIEW = r"""
import os,shutil,sys,tarfile,tempfile
from pathlib import Path,PurePosixPath
root=Path('/workspace/skills')
if root.is_symlink():
 raise RuntimeError('Skill view must not be a symlink')
root.mkdir(parents=True,exist_ok=True)
files={}
with tarfile.open(sys.argv[1],'r:gz') as archive:
 for entry in archive.getmembers():
  name=PurePosixPath(entry.name)
  if not entry.isfile() or name.is_absolute() or '..' in name.parts or len(name.parts)<2:
   raise RuntimeError('Invalid skill archive member')
  files[name.as_posix()]=(archive.extractfile(entry).read(),entry.mode & 0o755)
dirs={parent.as_posix() for name in files for parent in PurePosixPath(name).parents if str(parent)!='.'}
# Remove only entries in this sandbox's skill view, never following symlinks.
for base,children,names in os.walk(root,topdown=False,followlinks=False):
 for name in names+children:
  path=Path(base)/name
  rel=path.relative_to(root).as_posix()
  if path.is_symlink() or (rel not in files and rel not in dirs):
   if path.is_symlink() or not path.is_dir(): path.unlink()
   else: shutil.rmtree(path)
for name in sorted(dirs,key=lambda value:(len(PurePosixPath(value).parts),value)):
 path=root/name
 if path.exists() and not path.is_dir(): path.unlink()
 path.mkdir(exist_ok=True)
for name,(content,mode) in files.items():
 path=root/name
 if path.is_dir(): shutil.rmtree(path)
 if path.is_file() and path.read_bytes()==content:
  path.chmod(mode)
  continue
 with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as stream:
  temporary=Path(stream.name)
  stream.write(content)
 try:
  temporary.chmod(mode)
  os.replace(temporary,path)
 finally:
  temporary.unlink(missing_ok=True)
"""


def _installed_key(sandbox) -> str:
    return "sandbox:skill-view:" + str(sandbox.id)


async def _installed_digest(sandbox) -> str | None:
    from core.infra.ephemeral import get_ephemeral_state
    try:
        return await get_ephemeral_state().get(_installed_key(sandbox))
    except Exception as exc:  # noqa: BLE001 - unknown state only costs a re-send
        logger.warning("[opensandbox] skill view state unavailable: %s", type(exc).__name__)
        return None


async def stage_skill_view(sandbox, request):
    """Bring the sandbox's skill directory to the current authorized view.

    The view is recomputed for every command, so revocations apply immediately.
    It is only transferred when it differs from the view last installed in this
    sandbox. That record lives in the shared ephemeral store keyed by sandbox id,
    so every worker attached to the sandbox sees the same installed view; it is
    cleared before a transfer and written only after the transfer succeeds.
    """
    from core.config.settings import settings
    from core.infra.ephemeral import get_ephemeral_state

    from core.sandbox.providers.opensandbox.runtime import sandbox_lock
    async with sandbox_lock(sandbox, _LOCK_ATTR):
        files = await asyncio.to_thread(_view_files, request.user_id)
        digest = _view_digest(request.user_id, files)
        if await _installed_digest(sandbox) == digest:
            return
        state = get_ephemeral_state()
        await state.drop(_installed_key(sandbox))
        content = await asyncio.to_thread(_view_archive, files)
        from opensandbox.models.execd import RunCommandOpts

        source = "/tmp/.jx-skills-" + uuid.uuid4().hex + ".tgz"
        await sandbox.files.write_file(source, content)
        try:
            result = await sandbox.commands.run(
                "python3 -c " + shlex.quote(_SYNC_VIEW) + " " + shlex.quote(source),
                opts=RunCommandOpts(uid=str(SANDBOX_RUN_UID), gid=str(SANDBOX_RUN_GID), timeout=timedelta(seconds=60)))
            if getattr(result, "exit_code", None) != 0:
                raise SandboxError("Failed to synchronize authorized skill view")
        finally:
            await sandbox.commands.run("rm -f -- " + shlex.quote(source), opts=RunCommandOpts(timeout=timedelta(seconds=10)))
        await state.put(_installed_key(sandbox), digest, ttl=settings.sandbox.idle_ttl_s)


async def forget_skill_view(sandbox) -> None:
    """Force the next command to re-send the view (e.g. after a sandbox is reset for reuse)."""
    from core.infra.ephemeral import get_ephemeral_state
    await get_ephemeral_state().drop(_installed_key(sandbox))
