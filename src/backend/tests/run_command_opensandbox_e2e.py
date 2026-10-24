"""Real OpenSandbox background, process-adoption and recovery E2E."""
import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

async def verify():
    from core.db.engine import Base, SessionLocal, engine
    from core.db.models import ChatSession, UserShadow
    from core.sandbox.commands.runtime import CommandRuntime
    from core.sandbox.protocol import ProcessRequest
    from core.sandbox.providers.opensandbox.provider import OpenSandboxProvider
    from core.sandbox.commands import store
    Base.metadata.create_all(engine)
    session = "command-e2e-" + uuid.uuid4().hex
    user = "api_ak_command_e2e"
    with SessionLocal() as db:
        db.add(UserShadow(user_id=user, username="synthetic"))
        db.flush()
        db.add(ChatSession(chat_id=session, user_id=user, title="Command E2E"))
        db.commit()
    provider = OpenSandboxProvider()
    owner, successor = CommandRuntime(), CommandRuntime()
    def request(command):
        return ProcessRequest(script_content=command, script_name="_e2e.sh",
            language="bash", session_id=session, user_id=user)
    try:
        result = await owner.start(provider,
            request("printf x >> /workspace/launches; printf first; sleep 12; printf second"),
            chat_id=session, source_run_id=None, background=True)
        assert result["status"] == "running"
        task_id = result["task_id"]
        reference = store.snapshot(task_id)["execution_ref"]
        assert reference["provider"] == "opensandbox"
        independent = await owner.start(provider, request("printf independent"),
            chat_id=session, source_run_id=None)
        assert independent["stdout"] == "independent"
        await owner.shutdown()
        assert store.snapshot(task_id)["lease_until"] == 0
        await successor.recover(provider)
        await successor.wait(task_id, user, session, 30000)
        recovered = store.read(task_id, user, session, consumer=None, consume_notice=False)
        assert recovered["task_status"] == "completed", recovered
        assert recovered["stdout"] == "firstsecond", recovered
        marker = await provider.run_to_completion(request("cat /workspace/launches"))
        assert marker.stdout == "x"
        original = await provider.start_process(request("printf adopted; sleep 30"), yield_time_ms=250)
        adopted = await successor.adopt(provider, original["session_id"], user_id=user,
            session_id=session, chat_id=session, source_run_id=None)
        await successor.cancel(adopted["task_id"], user, session)
        await successor.wait(adopted["task_id"], user, session, 15000)
        assert store.snapshot(adopted["task_id"])["status"] == "cancelled"
        print(json.dumps({"result":"passed","provider":"opensandbox",
            "checks":["background plus independent command","same-execution recovery",
                      "persisted log cursor without replay","single execution marker",
                      "adopt existing handle and cancel"]}))
    finally:
        await owner.shutdown()
        await successor.shutdown()
        await provider.close_session(session)
        await provider.shutdown()

def main():
    image = os.environ["COMMAND_E2E_OPENSANDBOX_IMAGE"]
    domain = os.environ["COMMAND_E2E_OPENSANDBOX_DOMAIN"]
    with tempfile.TemporaryDirectory(prefix="command-cloud-e2e-") as folder:
        env = {"DATABASE_URL":"sqlite:///" + str(Path(folder)/"test.db"),
            "HUGAGENT_HOME":folder,"STORAGE_PATH":os.environ["COMMAND_E2E_STORAGE_PATH"],
            "DEPLOY_PROFILE":"team","AUTH_MODE":"mock","SANDBOX_PROVIDER":"opensandbox",
            "OPENSANDBOX_DOMAIN":domain,"OPENSANDBOX_IMAGE":image,
            "OPENSANDBOX_DIRECT_EXECD":"false","OPENSANDBOX_MYSPACE_BIND_MOUNT_ENABLED":"false",
            "OPENSANDBOX_SNAPSHOT_ENABLED":"false","HOST_STORAGE_PATH":os.environ["COMMAND_E2E_HOST_STORAGE_PATH"],
            "SCRIPT_RUNNER_WORKSPACE":"/workspace","REDIS_URL":"",
            "NO_PROXY":"127.0.0.1,localhost","no_proxy":"127.0.0.1,localhost"}
        for name in ("DWS","LARK","EMAIL","YIDA"):
            env[name + "_CREDS_BIND_MOUNT_ENABLED"] = "false"
        for name in ("JUPYTER","LIGHT"):
            env["OPENSANDBOX_POOL_" + name + "_MIN_IDLE"] = "0"
        os.environ.update(env)
        sys.path.insert(0, str(ROOT/"src/backend"))
        asyncio.run(verify())

if __name__ == "__main__":
    main()
