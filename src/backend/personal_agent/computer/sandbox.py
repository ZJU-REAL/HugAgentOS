"""Permanent OpenSandbox binding; ordinary pools never own these instances."""
import asyncio
import os
import base64
import json
import shlex
import uuid
from datetime import timedelta
from contextlib import asynccontextmanager
from pathlib import Path
from sqlalchemy import select
from core.db.engine import SessionLocal
from core.sandbox.providers.opensandbox.provider import OpenSandboxProvider
from core.sandbox.providers.opensandbox.models import SandboxSessionRecord as _Session
from core.sandbox.errors import SandboxError
from personal_agent.models import PersonalAgent
from core.db.threads import off_loop
from core.db.threads import run_in_session

# Layout of the permanent computer image (docker/personal-agent/Dockerfile.personal-agent and its entrypoint).
SANDBOX_PYTHON = "/opt/python/current/bin/python3"
DEFAULT_IMAGE = "hugagent-personal-agent:v5"
DESKTOP_HOME = "/home/pa-desktop"
DESKTOP_PRIVATE = "/run/pa-desktop"
# Desktop input runs as the desktop user; task Bash runs as UID 1000 and cannot read its files.
DESKTOP_RUN = {"uid": 1001, "gid": 1001, "working_directory": DESKTOP_HOME,
               "envs": {"EXECD_ACCESS_TOKEN": "", "DISPLAY": ":99", "XAUTHORITY": DESKTOP_PRIVATE + "/.Xauthority", "HOME": DESKTOP_HOME}}
STREAM_PORT = 6090

def controller():
    """The authenticated Personal Agent sandbox controller: (domain, api_key)."""
    domain = os.getenv("PERSONAL_AGENT_OPENSANDBOX_DOMAIN", "").strip()
    key = os.getenv("PERSONAL_AGENT_OPENSANDBOX_API_KEY", "").strip()
    if not domain or not key:
        raise SandboxError("Configure an authenticated Personal Agent sandbox controller on a separate compute network")
    return domain, key

async def install_once(provider, box, uid, name, digest, command):
    """Run a root install command once per computer and asset version. Concurrent callers wait."""
    import secrets
    from opensandbox.models.execd import RunCommandOpts
    from core.infra.ephemeral import get_ephemeral_state
    session = await provider._get_or_create_session(box, uid)
    state = get_ephemeral_state()
    ready = "pa:" + name + "-ready:" + session.sandbox.id
    if await state.get(ready) == digest: return
    lock, token = ready + ":install", secrets.token_hex(16)
    for _ in range(120):
        if await state.hold(lock, token, ttl=60): break
        await asyncio.sleep(0.25)
        if await state.get(ready) == digest: return
    else:
        raise SandboxError("Cloud " + name + " installation busy")
    try:
        if await state.get(ready) == digest: return
        result = await session.sandbox.commands.run(command, opts=RunCommandOpts(uid=0, gid=0, working_directory="/", timeout=timedelta(seconds=30)))
        if result.error: raise SandboxError("Cloud " + name + " installation failed")
        await state.put(ready, digest, ttl=86400)
    finally:
        await state.release(lock, token)

def _computer_owner(db, session_id):
    return db.scalar(select(PersonalAgent.user_id).where(PersonalAgent.sandbox_session_id == session_id))

class PersonalSandboxProvider(OpenSandboxProvider):

    def _sandbox_admission(self):
        """Computers live on their own controller, so they have their own capacity ledger."""
        from core.config.environment import _int
        from core.sandbox.providers.opensandbox.admission import admission_for
        domain, _ = controller()
        limit = max(1, _int(os.getenv("PERSONAL_AGENT_SANDBOX_MAX_TOTAL", "20"), 20))
        return admission_for(domain, limit=limit, config_factory=self._make_config)

    def _make_config(self):
        from opensandbox.config import ConnectionConfig
        domain, key = controller()
        return ConnectionConfig(domain=domain, api_key=key, use_server_proxy=True, request_timeout=timedelta(seconds=self._request_timeout_s))

    def _computer_config(self, ident):
        import secrets
        from core.infra.crypto import decrypt_secret, encrypt_secret
        with SessionLocal() as db:
            row = db.scalar(select(PersonalAgent).where(PersonalAgent.id == ident).with_for_update())
            if not row.execd_secret_enc:
                row.execd_secret_enc = encrypt_secret(secrets.token_urlsafe(48))
                db.commit()
            token = decrypt_secret(row.execd_secret_enc)
        config = self._make_config()
        config.headers = {**config.headers, "X-EXECD-ACCESS-TOKEN": token}
        return config, token

    async def _repoint_execd_direct(self, sandbox):
        # Dedicated compute network is reached through its authenticated controller.
        return None

    def _binding(self, session_id, user_id=None):
        with SessionLocal() as db:
            row = db.scalar(select(PersonalAgent).where(PersonalAgent.sandbox_session_id == session_id))
            if row is None or not user_id or row.user_id != str(user_id):
                raise SandboxError("Personal sandbox owner mismatch")
            return row.id, row.sandbox_id, row.generation

    @asynccontextmanager
    async def _session_use(self, session_id, user_id=None):
        """Use the permanent DB binding, never the ordinary pooled registry.

        Resolution already locks and checks computer ownership and mount grants.
        Permanent instances are never parked or recycled by another chat. Do not
        hold that lock across I/O: staging calls this resolver again for each file.
        """
        yield await self._get_or_create_session(session_id, user_id)

    async def _get_or_create_session(self, session_id, user_id=None):
        ident, sandbox_id, generation = self._binding(session_id, user_id)
        lock = await self._get_session_lock(session_id)
        async with lock:
            cached = self._sessions.get(session_id)
            if cached and cached.sandbox.id == sandbox_id:
                try:
                    await self._check_mount_revision(cached.sandbox, user_id)
                    return cached
                except Exception as exc:
                    if getattr(exc, "status_code", None) != 404:
                        raise
            if cached:
                self._sessions.pop(session_id, None)
                await cached.sandbox.close()
            if sandbox_id:
                from opensandbox import Sandbox
                try:
                    sandbox = await Sandbox.connect(sandbox_id, connection_config=self._computer_config(ident)[0], skip_health_check=True)
                    await self._check_mount_revision(sandbox, user_id)
                except Exception as exc:
                    # Only a confirmed missing instance permits recreation. Network errors preserve identity.
                    if getattr(exc, "status_code", None) != 404: raise
                    await self._check_mount_revision(sandbox, user_id)
                    def _drop_lost_sandbox(db):
                        row = db.get(PersonalAgent, ident)
                        if row.sandbox_id == sandbox_id:
                            row.sandbox_id = None
                            row.generation += 1
                            row.computer_status = "recovering"
                            db.commit()
                    await run_in_session(_drop_lost_sandbox)
                    _, sandbox_id, generation = self._binding(session_id, user_id)
                    sandbox = await self._provision(ident, session_id, user_id, generation)
            else:
                sandbox = await self._provision(ident, session_id, user_id, generation)
            await self._repoint_execd_direct(sandbox)
            session = _Session(sandbox=sandbox,
                               user_id=user_id, setup_done=True, pool_source="none")
            self._sessions[session_id] = session
            return session

    async def _check_mount_revision(self, sandbox, user_id):
        from core.files.spaces.sandbox import current_revision
        from core.files.spaces.mounts import MOUNT_REVISION_TAG
        info = await sandbox.get_info()
        revision = await asyncio.to_thread(current_revision, user_id)
        if (info.metadata or {}).get(MOUNT_REVISION_TAG) != revision:
            await sandbox.kill()
            await self._sandbox_admission().release(str(sandbox.id))
            raise SandboxError("File permissions changed; reopen the computer")
        from core.files.spaces.recovery import ensure_mounts
        import os
        await ensure_mounts(sandbox, user_id, info,
            domain=os.environ["PERSONAL_AGENT_OPENSANDBOX_DOMAIN"],
            api_key=os.getenv("PERSONAL_AGENT_OPENSANDBOX_API_KEY", ""))


    async def _provision(self, ident, session_id, user_id, generation):
        from core.files.spaces.leases import async_space_lock
        async with async_space_lock(user_id):
            return await self._provision_locked(ident, session_id, user_id, generation)

    async def _provision_locked(self, ident, session_id, user_id, generation):
        from opensandbox import Sandbox
        from opensandbox.models.sandboxes import Host, Volume
        from core.infra.ephemeral import get_ephemeral_state
        import uuid
        token = uuid.uuid4().hex
        key = "pa:provision:" + ident
        state = get_ephemeral_state()
        if not await state.hold(key, token, ttl=180):
            raise SandboxError("Permanent computer is being provisioned; retry shortly")
        try:
            # Recheck after acquiring the cross-worker creation lock.
            _, existing, _ = self._binding(session_id, user_id)
            if existing:
                sandbox = await Sandbox.connect(existing, connection_config=self._computer_config(ident)[0], skip_health_check=True)
                await self._check_mount_revision(sandbox, user_id)
                return sandbox
            from opensandbox import SandboxManager
            from opensandbox.models.sandboxes import SandboxFilter
            manager = await SandboxManager.create(self._make_config())
            try:
                matches = await manager.list_sandbox_infos(SandboxFilter(page_size=10,
                    metadata={"hugagent-owner": "personal-agent", "personal-agent-id": ident, "generation": str(generation)}))
                if len(matches.sandbox_infos) > 1: raise SandboxError("Permanent computer identity is ambiguous")
                if matches.sandbox_infos:
                    found = matches.sandbox_infos[0]
                    if found.status.state not in {"RUNNING", "PENDING"}: raise SandboxError("Permanent computer requires recovery")
                    sandbox = await Sandbox.connect(found.id, connection_config=self._computer_config(ident)[0], skip_health_check=True)
                    await self._check_mount_revision(sandbox, user_id)
                    def _mark_ready(db):
                        row = db.get(PersonalAgent, ident)
                        row.sandbox_id, row.computer_status = sandbox.id, "ready"
                        db.commit()
                    await run_in_session(_mark_ready)
                    return sandbox
            finally: await manager.close()
            root = os.getenv("PERSONAL_AGENT_VOLUME_ROOT", "").strip()
            if not root or not Path(root).is_absolute():
                raise SandboxError("PERSONAL_AGENT_VOLUME_ROOT must be an accessible absolute host path")
            local_root = os.getenv("PERSONAL_AGENT_LOCAL_VOLUME_ROOT", root)
            if not Path(local_root).is_absolute(): raise SandboxError("Personal computer local volume path must be absolute")
            work = Path(local_root) / ident / "workspace"
            work.mkdir(parents=True, exist_ok=True)
            os.chmod(work, 0o700)
            # Persona files (AGENTS.md, SOUL.md, IDENTITY.md, USER.md ...) exist before the first task.
            from personal_agent.workspace.persona import ensure_seeded
            ensure_seeded(ident)
            # Workspace data survives sandbox replacement.
            volume = Volume(name="personal-workspace", host=Host(path=str(Path(root) / ident / "workspace")), mount_path="/workspace", read_only=False)
            desktop = Path(local_root) / ident / "desktop"
            if not desktop.exists():
                desktop.mkdir(parents=True)
                os.chmod(desktop, 0o700)
            desktop_volume = Volume(name="personal-desktop", host=Host(path=str(Path(root) / ident / "desktop")), mount_path=DESKTOP_HOME, read_only=False)
            from core.files.spaces.mounts import provision_volumes
            space_volumes, space_metadata = await asyncio.to_thread(provision_volumes, user_id)
            computer_config, access_token = self._computer_config(ident)
            sandbox = await self._sandbox_admission().create(
                lambda metadata: Sandbox.create(os.getenv("PERSONAL_AGENT_IMAGE", DEFAULT_IMAGE), connection_config=computer_config, env={"EXECD_ACCESS_TOKEN": access_token},
                    timeout=None, skip_health_check=False, volumes=[volume, desktop_volume, *space_volumes],
                    metadata=metadata, entrypoint=["/bin/bash", "/opt/personal-agent/start.sh"]),
                metadata={"hugagent-owner": "personal-agent", "hugagent-user-id": user_id, "personal-agent-id": ident, "generation": str(generation), **space_metadata})
            def _mark_ready(db):
                row = db.get(PersonalAgent, ident)
                row.sandbox_id, row.computer_status = sandbox.id, "ready"
                db.commit()
            await run_in_session(_mark_ready)
            return sandbox
        finally:
            # Only the token owner releases; do not delete a successor's lock.
            await state.compare_exchange(key, token, "released", ttl=1)

    async def _persistent_setup(self, sess, req):
        return None

    async def _sync_inputs(self, sess, req):
        for group, encoded in ((req.resource_files, False), (req.input_files, False), (req.input_files_b64, True)):
            import base64
            for path, content in (group or {}).items():
                await self.put_file(req.session_id, path, base64.b64decode(content) if encoded else content, user_id=req.user_id)

    async def _file_command(self, session_id, user_id, path, source=None):
        from opensandbox.models.execd import RunCommandOpts
        from personal_agent.workspace.paths import workspace_path
        workspace_path(path)
        sess = await self._get_or_create_session(session_id, user_id)
        # Every directory and the final file use O_NOFOLLOW. Files run as UID 1000,
        # so a task cannot read the desktop profile even during a path race.
        code = """import os,sys,base64,json,stat
parts=sys.argv[1].split('/')[2:]
fd=os.open('/workspace',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
try:
 for part in parts[:-1]:
  if len(sys.argv)>2:
   try: os.mkdir(part,0o700,dir_fd=fd)
   except FileExistsError: pass
  new=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
  os.close(fd);fd=new
 mode=os.O_WRONLY|os.O_CREAT|os.O_TRUNC if len(sys.argv)>2 else os.O_RDONLY
 out=os.open(parts[-1],mode|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=fd)
 if not stat.S_ISREG(os.fstat(out).st_mode):
  os.close(out)
  raise ValueError('Only regular workspace files are supported')
 with os.fdopen(out,'wb' if len(sys.argv)>2 else 'rb') as f:
  if len(sys.argv)>2:
   with open(sys.argv[2],'rb') as src: f.write(src.read(20971521))
   print('{}')
  else:
   data=f.read(20971521)
   if len(data)>20971520: raise ValueError('File exceeds 20 MiB')
   print(json.dumps({'data':base64.b64encode(data).decode()}))
finally: os.close(fd)
"""
        command = SANDBOX_PYTHON + " -c " + shlex.quote(code) + " " + shlex.quote(path)
        if source: command += " " + shlex.quote(source)
        result = await sess.sandbox.commands.run(command, opts=RunCommandOpts(uid=1000, gid=1000,
            working_directory="/workspace", envs={"EXECD_ACCESS_TOKEN": ""}, timeout=timedelta(seconds=30)))
        if result.error: raise SandboxError("Workspace file operation failed")
        return json.loads("".join(str(item.text) for item in result.logs.stdout))

    async def get_file(self, session_id, path, user_id=None):
        value = await self._file_command(session_id, user_id, path)
        return base64.b64decode(value["data"])

    async def put_file(self, session_id, path, content, user_id=None):
        content = content.encode() if isinstance(content, str) else content
        if len(content) > 20 * 1024 * 1024: raise SandboxError("File exceeds 20 MiB")
        sess = await self._get_or_create_session(session_id, user_id)
        temporary = "/tmp/pa-transfer-" + uuid.uuid4().hex
        from opensandbox.models.execd import RunCommandOpts
        try:
            await sess.sandbox.files.write_file(temporary, content)
            await sess.sandbox.commands.run("chown 1000:1000 " + temporary + " && chmod 600 " + temporary,
                opts=RunCommandOpts(timeout=timedelta(seconds=10)))
            await self._file_command(session_id, user_id, path, source=temporary)
        finally:
            await sess.sandbox.commands.run("rm -f " + temporary, opts=RunCommandOpts(timeout=timedelta(seconds=10)))

    async def get_file_to_path(self, session_id, remote_path, local_path, *, max_bytes, user_id=None):
        content = await self.get_file(session_id, remote_path, user_id=user_id)
        if len(content) > max_bytes: raise SandboxError("File exceeds transfer limit")
        Path(local_path).write_bytes(content)
        return len(content)

    async def ensure_user_workspace(self, session_id, user_id):
        await self._get_or_create_session(session_id, user_id)

    async def close_session(self, session_id):
        # A chat close is never an explicit permanent-computer deletion.
        return None

    async def reap_idle_sessions(self):
        return 0

    async def touch_session(self, session_id):
        uid = await run_in_session(_computer_owner, session_id)
        if uid is None: return False
        sess = await self._get_or_create_session(session_id, uid)
        try:
            info = await sess.sandbox.get_info()
        except Exception as exc:
            if getattr(exc, "status_code", None) != 404: raise
            self._sessions.pop(session_id, None)
            await sess.sandbox.close()
            sess = await self._get_or_create_session(session_id, uid)
            info = await sess.sandbox.get_info()
        if info.expires_at is not None:
            raise SandboxError("Permanent computer unexpectedly has an expiration time")
        def _mark_ready(db):
            row = db.scalar(select(PersonalAgent).where(PersonalAgent.sandbox_session_id == session_id))
            row.computer_status = "ready"
            db.commit()
        await run_in_session(_mark_ready)
        return True

    @off_loop
    def current_sandbox_id(self, session_id):
        with SessionLocal() as db:
            row = db.scalar(select(PersonalAgent).where(PersonalAgent.sandbox_session_id == session_id))
            return row.sandbox_id if row else None

_provider = None

def get_personal_sandbox_provider():
    global _provider
    if _provider is None: _provider = PersonalSandboxProvider()
    return _provider
