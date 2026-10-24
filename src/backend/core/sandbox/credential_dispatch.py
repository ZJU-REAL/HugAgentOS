"""Provider-neutral credential dispatch for account-owned cloud commands."""
from core.harness.registry import sandbox_credentials

def credential_environment():
    environment = {}
    for provider in sandbox_credentials():
        if provider.environment:
            environment.update(provider.environment())
    return environment


def credential_mounts(user_id):
    return [mount for provider in sandbox_credentials() if provider.mounts
            for mount in provider.mounts(user_id)]


async def prepare_credentials(host, sandbox, user_id, script):
    if not user_id:
        return
    for provider in sorted(sandbox_credentials(), key=lambda item: item.prepare_order):
        if provider.before_command:
            await provider.before_command(host, sandbox, user_id, script)


async def persist_credentials(host, sandbox, user_id, script):
    if not user_id:
        return
    for provider in sandbox_credentials():
        if provider.after_command:
            await provider.after_command(host, sandbox, user_id, script)
