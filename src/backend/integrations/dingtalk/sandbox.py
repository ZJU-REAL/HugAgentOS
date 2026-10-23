"""Credential contribution for dingtalk."""
from core.harness.registry import SandboxCredentials, register_sandbox_credentials
from integrations.dingtalk.cube_credentials import CubeCredentials
from integrations.dingtalk.mounts import mounts
from integrations.dingtalk.environment import dws_extra_envs

def state(host):
    return host.credential_state("dingtalk", lambda: CubeCredentials(host.credential_request_timeout))

async def prepare(host, sandbox, user_id, script):
    if "dws" in (script or ""):
        await state(host).ensure(sandbox, user_id)

def register():
    register_sandbox_credentials(SandboxCredentials("dingtalk", environment=dws_extra_envs, mounts=mounts, before_command=prepare, prepare_order=0))
