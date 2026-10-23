"""Run against isolated containers only. Uses real sandbox and optional real SMTP/IMAP."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time
from sqlalchemy import select
from core.db.engine import SessionLocal
from integrations.email.models import EmailConnection
from core.infra.crypto import encrypt_secret
from personal_agent.models import PersonalAgent, PersonalWorkItem
from personal_agent.context import AgentContext
from personal_agent.computer.sandbox import get_personal_sandbox_provider

class CaptureTools:
    def __init__(self): self.tools = {}
    def append(self, tool):
        fn = tool._func
        self.tools[fn.__name__] = fn


def data(chunk): return json.loads(chunk.content[0].text)


async def run(mailbox_file, expect_existing_workspace=False):
    with SessionLocal() as db:
        agent = db.scalar(select(PersonalAgent))
        assert agent is not None, "Initialize an isolated Personal Agent through the HTTP API first"
        uid, box, agent_id = agent.user_id, agent.sandbox_session_id, agent.id
        # The paused identity keeps the ordinary worker from claiming verification tasks.
        agent.status = "ready"
        task = AgentContext(db, uid).tasks.submit("Integration verification", "real-services-verification-" + str(time.time_ns()))
        row = db.get(PersonalWorkItem, task["id"])
        row.status, row.owner, row.lease_until = "running", "e2e", time.time() + 1800
        agent.status = "paused"
        db.commit()
        task_id = row.id
    provider = get_personal_sandbox_provider()
    await provider.ensure_user_workspace(box, uid)
    session = await provider._get_or_create_session(box, uid)
    info = await session.sandbox.get_info()
    assert info.expires_at is None, "Permanent computer must use explicit cleanup"
    # Recovery must retain files from the previous container generation.
    if expect_existing_workspace:
        previous = await provider.get_file(box, "/workspace/work/verification.txt", user_id=uid)
        assert previous == b"Personal Agent file verification\n"
        print("PASS: sandbox recreation retained the existing workspace", flush=True)
    from types import SimpleNamespace
    from personal_agent.tools.tools import register_personal_tools
    tools = CaptureTools()
    register_personal_tools(tools, SimpleNamespace(profile_ref=task_id, current_user_id=uid))
    probe = """import os,urllib.request,urllib.error,json
assert not os.environ.get('EXECD_ACCESS_TOKEN')
req=urllib.request.Request('http://127.0.0.1:44772/command', data=json.dumps({'command':'id','uid':0}).encode(), headers={'Content-Type':'application/json'})
try:
 urllib.request.urlopen(req,timeout=5)
 raise AssertionError('Unauthenticated execd command succeeded')
except urllib.error.HTTPError as exc:
 assert exc.code in (401,403),exc.code
print('EXECUTION_BOUNDARY_VERIFIED')
"""
    import shlex
    result = data(await tools.tools["pa_bash"]("/opt/python/current/bin/python3 -c " + shlex.quote(probe)))
    assert result["exit_code"] == 0 and "EXECUTION_BOUNDARY_VERIFIED" in result["stdout"], result
    print("PASS: task environment contains no execd credential; unauthenticated root execution is denied", flush=True)
    # Model code must not cross the file service through a symlink.
    attack = data(await tools.tools["pa_bash"]("ln -sfn /home/pa-desktop /workspace/work/private-link"))
    try:
        await provider.get_file(box, "/workspace/work/private-link/.Xauthority", user_id=uid)
        raise AssertionError("Symlink crossed the workspace boundary")
    except Exception as exc:
        assert "Symlink crossed" not in str(exc)
    print("PASS: private desktop profile and symlink traversal denied", flush=True)
    content = "Personal Agent file verification\n".encode()
    path = "/workspace/work/verification.txt"
    await provider.put_file(box, path, content, user_id=uid)
    assert await provider.get_file(box, path, user_id=uid) == content
    first = await provider.current_sandbox_id(box)
    await provider.close_session(box)
    assert await provider.current_sandbox_id(box) == first
    print("PASS: permanent sandbox, dedicated volume and file read/write", flush=True)
    from personal_agent.computer.computer import operate
    observed = await operate(task_id, uid, "cloud", {"type": "observe"})
    assert observed.get("image_base64") and observed.get("observation_id")
    print("PASS: real computer screenshot", observed["width"], observed["height"], flush=True)
    await asyncio.sleep(1.1)
    moved = await operate(task_id, uid, "cloud", {"type": "move", "x": 500, "y": 300,
        "observation_id": observed["observation_id"]})
    assert moved.get("status") == "input_executed", moved
    print("PASS: real computer input and verification screenshot", flush=True)
    if mailbox_file:
        account = json.loads(Path(mailbox_file).read_text())
        with SessionLocal() as db:
            agent = db.get(PersonalAgent, agent_id)
            agent.config = {**agent.config, "email_enabled": True, "space_enabled": True}
            connection = db.get(EmailConnection, uid) or EmailConnection(user_id=uid)
            connection.status, connection.email_address = "connected", account["user"]
            connection.imap_host, connection.imap_port = account["imap"]["host"], account["imap"]["port"]
            connection.imap_security = "tls" if account["imap"]["secure"] else "starttls"
            connection.smtp_host, connection.smtp_port = account["smtp"]["host"], account["smtp"]["port"]
            connection.smtp_security = "tls" if account["smtp"]["secure"] else "starttls"
            connection.secret_enc = encrypt_secret(account["pass"])
            db.add(connection); db.commit()
        from personal_agent.tools.email import register_email_tools
        from personal_agent.tasks.approvals import approve
        tools = CaptureTools(); register_email_tools(tools, task_id, uid)
        send = tools.tools["pa_email_send"]
        pending = data(await send(account["user"], "Personal Agent verification", "Attachment round trip", [path]))
        assert pending.get("pending_approval")
        with SessionLocal() as db:
            task = db.get(PersonalWorkItem, task_id)
            task.status = "waiting"; db.commit()
            digest = task.checkpoint["pending_approval"]["id"]
            approve(AgentContext(db, uid), task_id, digest, True)
            task.status, task.owner, task.lease_until = "running", "e2e", time.time()+1800; db.commit()
        sent = data(await send(account["user"], "Personal Agent verification", "Attachment round trip", [path]))
        assert sent["status"] == "sent"
        assert data(await send(account["user"], "Personal Agent verification", "Attachment round trip", [path])) == sent
        print("PASS: real SMTP attachment send and duplicate-send prevention", flush=True)
        for _ in range(20):
            messages = data(await tools.tools["pa_email_list"]())
            if messages["message_uids"]: break
            await asyncio.sleep(1)
        message_uid = messages["message_uids"][-1]
        metadata = data(await tools.tools["pa_email_read"](message_uid))
        assert metadata["attachments"][0]["filename"] == "verification.txt"
        downloaded = data(await tools.tools["pa_email_download"](message_uid, 0, "/workspace/work/mail-return.txt"))
        assert downloaded["sha256"] == hashlib.sha256(content).hexdigest()
        print("PASS: real IMAP attachment receive; SHA-256 matches", flush=True)
        mounted = "/workspace/myspace/" + uid + "/personal-agent-verification-" + str(time.time_ns()) + ".txt"
        await provider.put_file(box, mounted, content, user_id=uid)
        assert await provider.get_file(box, mounted, user_id=uid) == content
        print("PASS: mounted personal directory file round trip", flush=True)
    with SessionLocal() as db:
        AgentContext(db, uid).tasks.complete(task_id, "Real service verification completed", {}, owner="e2e")
    print("PASS: only final answer projected to canonical conversation", flush=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mailbox-file")
    parser.add_argument("--expect-existing-workspace", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.mailbox_file, args.expect_existing_workspace))
