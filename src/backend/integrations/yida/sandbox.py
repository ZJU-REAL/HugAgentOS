"""Credential contribution for yida."""
from core.harness.registry import SandboxCredentials, register_sandbox_credentials
from integrations.yida.cube_credentials import CubeCredentials
from integrations.yida.mounts import mounts

def state(host):
    return host.credential_state("yida", lambda: CubeCredentials(host.credential_request_timeout))

async def prepare(host, sandbox, user_id, script):
    if "openyida" in (script or ""):
        await state(host).ensure(sandbox, user_id)

async def persist(host, sandbox, user_id, script):
    if "openyida" in (script or ""):
        await state(host).persist(sandbox, user_id)

def register():
    register_sandbox_credentials(SandboxCredentials("yida", mounts=mounts, before_command=prepare, after_command=persist, prepare_order=3))
