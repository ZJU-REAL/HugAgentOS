"""Credential contribution for lark."""
from core.harness.registry import SandboxCredentials, register_sandbox_credentials
from integrations.lark.cube_credentials import CubeCredentials
from integrations.lark.mounts import mounts

def state(host):
    return host.credential_state("lark", lambda: CubeCredentials(host.credential_request_timeout))

async def prepare(host, sandbox, user_id, script):
    if "lark" in (script or ""):
        await state(host).ensure(sandbox, user_id)

def register():
    register_sandbox_credentials(SandboxCredentials("lark", mounts=mounts, before_command=prepare, prepare_order=2))
