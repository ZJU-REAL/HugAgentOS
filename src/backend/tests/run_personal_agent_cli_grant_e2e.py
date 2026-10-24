"""Run inside the isolated backend: real sandbox CLI and exact write approval."""

import asyncio
import json
import time
import uuid
from types import SimpleNamespace
from core.db.engine import SessionLocal
from core.db.models import UserShadow
from personal_agent.models import PersonalWorkItem
from personal_agent.context import AgentContext
from personal_agent.tools.tools import register_personal_tools
from personal_agent.tasks.worker import PersonalAgentWorker
from personal_agent.tasks.approvals import approve


class Tools:
    def __init__(self):
        self.functions = {}

    def append(self, tool):
        fn = tool._func
        self.functions[fn.__name__] = fn


def data(reply):
    return json.loads(reply.content[0].text)


async def main():
    uid = "user_3b49688e94224ee9"
    nonce = uuid.uuid4().hex
    with SessionLocal() as db:
        svc = AgentContext(db, uid)
        agent = svc.identity.agent()
        old_status = agent.status
        agent.status = "ready"
        db.commit()
        task = svc.tasks.submit("CLI grant verification", "grant-" + nonce)["id"]
        row = db.get(PersonalWorkItem, task)
        row.status = "running"
        row.owner = "grant-test"
        row.run_id = "grant-run-" + nonce
        row.lease_until = time.time() + 300
        agent.status = "ready"
        db.commit()
    toolkit = Tools()
    register_personal_tools(
        toolkit, SimpleNamespace(profile_ref=task, current_user_id=uid)
    )
    try:
        read = data(await toolkit.functions["pa_bash"]("hugagent device list"))
        assert read["exit_code"] == 0, read
        assert isinstance(json.loads(read["stdout"]), list)
        print(
            "PASS: task-scoped CLI reads devices from the actual permanent cloud computer",
            flush=True,
        )
        result = data(
            await toolkit.functions["pa_bash"](
                "hugagent --request-key grant-write-"
                + nonce
                + " chat new --title Grant-approval-verification"
            )
        )
        assert json.loads(result["stdout"])["pending_approval"], result
        with SessionLocal() as db:
            svc = AgentContext(db, uid)
            row = db.get(PersonalWorkItem, task)
            pending = row.checkpoint["pending_approval"]
            row.status = "waiting"
            db.commit()
            approve(svc, task, pending["id"], True)
            row.status = "running"
            row.run_id = "grant-resumed-" + nonce
            row.lease_until = time.time() + 300
            db.commit()
        await PersonalAgentWorker().dispatch_approved(task, uid)
        with SessionLocal() as db:
            row = db.get(PersonalWorkItem, task)
            receipt = row.checkpoint["action_receipts"][pending["id"]]
            assert receipt["status"] == "completed" and receipt["result"]["chat_id"], receipt
            chat_id = receipt["result"]["chat_id"]
        print(
            "PASS: exact session write approval resumes once and stores the cloud session receipt",
            flush=True,
        )
        from api.routes.v1.chats.sessions import delete_chat
        from core.auth.backend import UserContext

        with SessionLocal() as db:
            delete_chat(
                chat_id,
                user=UserContext(user_id=uid, user_center_id="pa-e2e-owner", username="owner"),
                db=db,
            )
    finally:
        with SessionLocal() as db:
            svc = AgentContext(db, uid)
            svc.identity.agent().status = old_status
            row = db.get(PersonalWorkItem, task)
            row.status = "cancelled"
            row.lease_until = None
            row.owner = None
            db.commit()


if __name__ == "__main__":
    asyncio.run(main())
