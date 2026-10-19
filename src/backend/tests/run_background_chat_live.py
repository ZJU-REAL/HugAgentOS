"""Acceptance against an authorized, deployed local chat/model stack."""
import argparse
import asyncio
import json
import os
import subprocess
import time
import uuid
from collections import Counter
from pathlib import Path
import httpx

def audit(state, chat_id):
    code = """
import json
from core.db.engine import SessionLocal
from core.db.models import ChatRun, ChatMessage, ToolCallLog, CommandTask
with SessionLocal() as db:
 runs=db.query(ChatRun).filter_by(chat_id=CHAT,user_id=USER).order_by(ChatRun.created_at).all()
 commands=db.query(CommandTask).filter_by(chat_id=CHAT,user_id=USER).all()
 tools=db.query(ToolCallLog).filter_by(chat_id=CHAT,user_id=USER).order_by(ToolCallLog.created_at).all()
 messages=db.query(ChatMessage).filter_by(chat_id=CHAT).order_by(ChatMessage.chat_seq).all()
 print("__AUDIT__"+json.dumps({
  "runs":[{"id":r.run_id,"status":r.status,"phase":r.run_phase,"kind":(r.request_payload or {}).get("kind"),
           "worker":r.lease_owner,"model":(r.recovery_snapshot or {}).get("worker_args",{}).get("model_name")} for r in runs],
  "commands":[{"id":r.task_id,"status":r.status,"notice":r.notice_status,"mode":r.delivery_mode,
              "command":r.command,"stdout":r.stdout,"stderr":r.stderr,"ref":r.execution_ref,
              "source_run":r.source_run_id,"notice_run":r.notice_run_id} for r in commands],
  "tools":[{"name":t.tool_name,"args":t.tool_args,"status":t.status,"source":t.source} for t in tools],
  "messages":[{"role":m.role,"seq":m.chat_seq,"content":m.content,
               "wake":(m.extra_data or {}).get("system_wake"),"model_steps":m.model_steps} for m in messages]
 },ensure_ascii=False,default=str))
"""
    code = "CHAT=" + repr(chat_id) + "\nUSER=" + repr(state["user_id"]) + "\n" + code
    result = subprocess.run(["docker","exec","-i","hugagent-backend","python","-"],
        input=code,text=True,capture_output=True,check=True)
    return json.loads(next(line[9:] for line in result.stdout.splitlines() if line.startswith("__AUDIT__")))

async def scenario(identity, name, resume=False):
    state = json.loads(identity.read_text())
    output = identity.parent
    suffix = uuid.uuid4().hex[:12]
    chat = "bg_accept_" + name + "_" + suffix
    marker = name.upper() + "_" + suffix
    duration = {"idle":45,"busy":8,"cancel":90,"restart":90}[name]
    command = ("mkdir -p /workspace/.background-e2e/" + suffix +
        "; printf x >> /workspace/.background-e2e/" + suffix + "/launches" +
        "; printf START_" + marker + "; sleep " + str(duration) + "; printf FINISH_" + marker)
    prompt = ("这是后台执行验收。请仅启动一次下面的 Bash 命令，并使用后台执行。"
        "任务完成后，在当前会话告诉我最终标准输出。不要改写命令。\n命令：" + command)
    if name == "busy":
        prompt += ("\n后台启动后，请立即再用一次前台 Bash 执行独立命令"
            " `sleep 15; printf INDEPENDENT_DONE`，yield_time_ms=30000。然后处理原任务结果。")
    if resume:
        saved = state["chats"][name]
        chat, marker, command = saved["chat_id"], saved["marker"], saved["command"]
    else:
        state["chats"][name] = {"chat_id":chat,"marker":marker,"command":command}
        identity.write_text(json.dumps(state))
        os.chmod(identity,0o600)
    started = time.monotonic()
    async with httpx.AsyncClient(base_url=state["base_url"],
        cookies={state["cookie_name"]:state["token"]},trust_env=False,timeout=240) as client:
        if not resume:
            request = asyncio.create_task(client.post("/v1/agents/responses",json={
                "chat_id":chat,"message":prompt,"stream":False,"chat_mode":"fast",
                "enabled_skills":[],"enabled_mcps":[],"enabled_kbs":[],"enabled_agents":[]}))
            response = await request
            (output/(name+"-response.json")).write_text(response.text)
            response.raise_for_status()
            rows = (await client.get("/v1/commands",params={"chat_id":chat})).json()["data"]
            first = next((row for row in rows if "printf x >>" in row["command"]), None)
            if first is None:
                raise AssertionError("Model did not start the requested command: " + response.text[:1000])
            initial_status = first["status"]
            print(json.dumps({"phase":"initial","scenario":name,"chat_id":chat,
                "seconds":round(time.monotonic()-started,2),"task_status":initial_status}),flush=True)
            if name in ("idle","restart","cancel"):
                assert initial_status == "running", "Model waited through the background execution"
            if name in ("cancel","restart"):
                (output/(name+"-audit.json")).write_text(json.dumps(audit(state,chat),ensure_ascii=False,indent=2))
                print(json.dumps({"phase":"ready_for_control","scenario":name,"chat_id":chat,
                    "task_id":first["task_id"]}),flush=True)
                return
        deadline = time.monotonic()+600
        while time.monotonic()<deadline:
            rows=(await client.get("/v1/commands",params={"chat_id":chat})).json()["data"]
            active=(await client.get("/v1/chats/"+chat+"/active-run")).json()["data"]
            if rows and all(row["status"] not in ("starting","running") for row in rows) and not active:
                result=audit(state,chat)
                if (result["commands"]
                        and all(row["notice"] in ("delivered","consumed","none") for row in result["commands"])
                        and all(row["status"] == "completed" for row in result["runs"])
                        and (name not in ("idle", "restart") or len(result["runs"]) >= 2)):
                    break
            await asyncio.sleep(3)
        else:
            raise AssertionError("Completion was not delivered within the acceptance window")
    result=audit(state,chat)
    (output/(name+"-audit.json")).write_text(json.dumps(result,ensure_ascii=False,indent=2))
    launches=[row for row in result["commands"] if "printf x >>" in row["command"]]
    assert len(launches)==1
    task=launches[0]
    assert task["status"]=="completed", task
    assert task["stdout"]=="START_"+marker+"FINISH_"+marker, task
    assert all(row["status"]=="completed" for row in result["runs"]), result["runs"]
    counts=Counter(row["name"] for row in result["tools"])
    assert counts["write_stdin"]<=2, counts
    assert all(row["status"]=="success" for row in result["tools"]), result["tools"]
    if name in ("idle", "restart"):
        assert len(result["runs"])==2, result["runs"]
        assert task["mode"]=="followup", task
    else:
        assert len(result["runs"])==1, result["runs"]
        assert task["mode"]=="boundary", task
        assert not any(row["wake"] for row in result["messages"])
    assert "FINISH_"+marker in "".join(row["content"] or "" for row in result["messages"] if row["role"]=="assistant")
    print(json.dumps({"result":"passed","scenario":name,"chat_id":chat,
        "run_count":len(result["runs"]),"tool_counts":dict(counts),
        "seconds":round(time.monotonic()-started,2)},ensure_ascii=False),flush=True)

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--identity",type=Path,required=True)
    parser.add_argument("--scenario",choices=("idle","busy","cancel","restart"),required=True)
    parser.add_argument("--resume",action="store_true")
    args=parser.parse_args()
    asyncio.run(scenario(args.identity,args.scenario,args.resume))
