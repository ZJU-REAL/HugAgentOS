"""Credential contribution for email."""
from core.harness.registry import SandboxCredentials, register_sandbox_credentials
from integrations.email.cube_credentials import CubeCredentials
from integrations.email.mounts import mounts

def state(host):
    return host.credential_state("email", lambda: CubeCredentials(host.credential_request_timeout))

async def prepare(host, sandbox, user_id, script):
    if "himalaya" in (script or ""):
        await state(host).ensure(sandbox, user_id)

def register():
    register_sandbox_credentials(SandboxCredentials("email", mounts=mounts, before_command=prepare, prepare_order=1))
