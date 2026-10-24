"""Run inside the cloud backend: a real task grant drives the real cloud-computer CLI.

The CLI manages ordinary and project chats on the cloud HugAgentOS and, through the
desktop device link, on a desktop execution plane (a local-mode backend started with
the identity bridge). Hand the device printed by ``setup`` to that backend with
``PUT /v1/device-link`` and enable session management there. Writes go through the
same task approval the user grants in the approval card. Never run against production.

  python tests/run_personal_agent_project_sessions_e2e.py setup <uid> <marker>
  python tests/run_personal_agent_project_sessions_e2e.py run <uid> <device_id> <cloud_project> <local_project> <marker>

<local_project> is a project on the device's HugAgentOS whose instructions end replies with <marker>-LOCAL.
  python tests/run_personal_agent_project_sessions_e2e.py cleanup <uid> <device_id> <cloud_project>
"""

import asyncio
import json
import shlex
import sys
import time
import uuid
from types import SimpleNamespace
from core.db.engine import SessionLocal
from personal_agent.tasks.approvals import approve
from personal_agent.models import PersonalWorkItem
from personal_agent.context import AgentContext
from personal_agent.tools.tools import register_personal_tools


class Tools:
    def __init__(self):
        self.functions = {}

    def append(self, tool):
        fn = tool._func
        self.functions[fn.__name__] = fn


def setup(uid, marker):
    from core.projects.edition import ProjectService
    from personal_agent.computer.devices import pair

    with SessionLocal() as db:
        device = pair(db, uid, "Project E2E local", "linux", ["hugagent.sessions"])
        svc = ProjectService(db)
        project = svc.create_personal(uid, "PA E2E Cloud Project " + marker[-6:], "e2e")
        project.instructions = "Always end every reply with the exact token " + marker + "-CLOUD."
        db.commit()
        print(json.dumps({"device": device, "project_id": project.project_id}))


def cleanup(uid, device_id, project_id):
    from core.db.models import Project
    from core.infra.time import utc_now
    from personal_agent.models import PersonalDevice

    with SessionLocal() as db:
        device = db.get(PersonalDevice, device_id)
        if device and device.user_id == uid:
            device.revoked = 1
        project = db.get(Project, project_id)
        if project and project.owner_user_id == uid:
            project.deleted_at = utc_now()
        db.commit()
    print("cleanup done")


class Harness:
    def __init__(self, uid):
        self.uid = uid
        self.toolkit = Tools()
        self.nonce = uuid.uuid4().hex[:10]

    def start_task(self):
        with SessionLocal() as db:
            agent = AgentContext(db, self.uid).identity.agent()
            assert agent and agent.status == "ready", "Personal Agent must be ready"
            # Created directly as running: the background worker never claims it, and no
            # test text enters the owner's lifelong conversation.
            row = PersonalWorkItem(
                id="pa_task_e2e_" + self.nonce,
                agent_id=agent.id,
                goal="Project session E2E",
                request_key="project-e2e-" + self.nonce,
                status="running",
                owner="project-e2e",
                run_id="project-e2e-run-" + self.nonce,
                lease_until=time.time() + 3600,
                checkpoint={},
            )
            db.add(row)
            db.commit()
            self.task = row.id
        register_personal_tools(
            self.toolkit, SimpleNamespace(profile_ref=self.task, current_user_id=self.uid)
        )

    def finish_task(self):
        with SessionLocal() as db:
            row = db.get(PersonalWorkItem, self.task)
            row.status, row.lease_until, row.owner = "cancelled", None, None
            db.commit()

    async def bash(self, command, timeout=90):
        reply = await self.toolkit.functions["pa_bash"](command, timeout_seconds=timeout)
        return json.loads(reply.content[0].text)

    async def cli(self, target, *args, timeout=90, approvals=None):
        """One CLI call in the cloud computer; a write waits once for the user's approval."""
        key = uuid.uuid4().hex
        command = "hugagent --target " + shlex.quote(target) + " --request-key " + key + " "
        command += " ".join(shlex.quote(a) for a in args)
        for _ in range(2):
            done = await self.bash(command, timeout)
            assert done["exit_code"] == 0, done
            result = json.loads(done["stdout"])
            if not result.get("pending_approval"):
                return result
            with SessionLocal() as db:
                service = AgentContext(db, self.uid)
                pending = db.get(PersonalWorkItem, self.task).checkpoint["pending_approval"]
                assert pending["action"]["target"] == target, pending
                if approvals is not None:
                    approvals.append(pending["action"])
                # The approval card: "allow this kind of action for this task".
                approve(service, self.task, pending["id"], True, scope="task")
        raise AssertionError("Approved write did not run: " + command)


def completed(job):
    assert job.get("status") == "completed", job
    result = job["result"]
    assert not (isinstance(result, dict) and result.get("error")), job
    return result


async def exercise(h, target, project_id, marker):
    approvals = []
    detail = completed(await h.cli(target, "project", "get", project_id))
    assert marker in (detail.get("instructions") or ""), detail
    listed = completed(await h.cli(target, "project", "list", "--query", detail["name"]))["items"]
    assert [p["project_id"] for p in listed] == [project_id], listed
    print("PASS", target, ": project list/get through the cloud-computer CLI", flush=True)

    created = completed(
        await h.cli(
            target, "chat", "new", "--project", project_id, "--title", "PA project E2E",
            approvals=approvals,
        )
    )
    chat_id = created["chat_id"]
    assert created["project_id"] == project_id, created
    assert approvals and approvals[0]["args"]["project_id"] == project_id, approvals
    in_project = completed(await h.cli(target, "project", "chats", project_id))["items"]
    assert chat_id in [c["chat_id"] for c in in_project], in_project
    print("PASS", target, ": chat created inside the project after approval", flush=True)

    # No --project on send: the backend must keep the chat's project context.
    sent = completed(
        await h.cli(
            target, "chat", "send", chat_id,
            "Reply with one short sentence about the weather. Do not use tools.",
        )
    )
    run_id = sent["run_id"]
    followed = await h.cli(target, "run", "follow", run_id, "--timeout", "240", timeout=280)
    assert completed(followed)["status"] == "completed", followed
    history = completed(await h.cli(target, "chat", "history", chat_id, "--order", "asc"))["items"]
    replies = [m["content"] for m in history if m["role"] == "assistant"]
    assert replies and marker in replies[-1], replies
    print("PASS", target, ": background project run finished with project instructions", flush=True)

    plain = completed(await h.cli(target, "chat", "new", "--title", "PA ordinary E2E"))
    assert not plain.get("project_id"), plain
    run_id = completed(
        await h.cli(
            target, "chat", "send", plain["chat_id"],
            "Write a 1500 word essay about software testing. Do not use tools.",
        )
    )["run_id"]
    active = completed(await h.cli(target, "run", "active", plain["chat_id"]))
    assert active and active.get("run_id") == run_id, active
    assert completed(await h.cli(target, "run", "steer", run_id, "Add one example."))["queued"]
    assert completed(await h.cli(target, "run", "cancel", run_id))["cancelled"]
    history = completed(await h.cli(target, "chat", "history", plain["chat_id"]))["items"]
    assert all(marker not in m["content"] for m in history), "ordinary chat must not use project"
    print("PASS", target, ": ordinary chat run started, steered and cancelled", flush=True)

    for ident in (chat_id, plain["chat_id"]):
        await h.cli(target, "chat", "delete", ident)
    gone = completed(await h.cli(target, "project", "chats", project_id))["items"]
    assert chat_id not in [c["chat_id"] for c in gone], gone
    print("PASS", target, ": test chats deleted", flush=True)


async def run(uid, device_id, cloud_project, local_project, marker):
    h = Harness(uid)
    h.start_task()
    try:
        devices = json.loads((await h.bash("hugagent device list"))["stdout"])
        device = next(d for d in devices if d["id"] == device_id)
        assert device["online"] and device["sessions_enabled"], device
        print("PASS: the cloud computer sees the paired local device online", flush=True)
        await exercise(h, "cloud", cloud_project, marker)
        await exercise(h, "device:" + device_id, local_project, marker)
    finally:
        h.finish_task()


if __name__ == "__main__":
    command, *values = sys.argv[1:]
    if command == "setup":
        setup(*values)
    elif command == "cleanup":
        cleanup(*values)
    else:
        asyncio.run(run(*values))
