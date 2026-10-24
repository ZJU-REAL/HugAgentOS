"""Task sessions, generated outputs, desktop ownership and local consent."""
import time
import pytest
from types import SimpleNamespace
from sqlalchemy import create_engine,select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from core.db.engine import Base
from core.db.models import UserShadow,ChatSession,ChatRun,Artifact
from personal_agent.models import PersonalWorkItem, PersonalDeviceCommand
from personal_agent.context import AgentContext
from personal_agent.conversation.message_routing import admit
from personal_agent.tasks import task_ops
from core.db.models import ChatMessage
from personal_agent.tasks.task_threads import history, Transcript
from personal_agent.delivery.outputs import listing

@pytest.fixture
def sessions(monkeypatch):
    engine=create_engine("sqlite://",connect_args={"check_same_thread":False},poolclass=StaticPool)
    Base.metadata.create_all(engine);factory=sessionmaker(bind=engine,expire_on_commit=False)
    monkeypatch.setattr("core.db.engine.SessionLocal",factory)
    with factory() as db:
        db.add_all([UserShadow(user_id=u,user_center_id=u,username=u) for u in ["owner","other"]]);db.commit()
    yield factory;engine.dispose()


def test_each_task_has_its_own_session_and_only_its_scoped_instructions(sessions):
    with sessions() as db:
        svc=AgentContext(db,"owner");a=svc.tasks.submit("部署 A","a")["id"];b=svc.tasks.submit("旅行 B","b")["id"]
        entry=admit(svc,"部署改分支，旅行换酒店","multi")
        # The agent's turn splits the message into one instruction per task.
        message=db.get(ChatMessage,entry["message_id"])
        task_ops.update_task(svc,message,a,"部署改分支");task_ops.update_task(svc,message,b,"旅行换酒店");db.commit()
        ha,hb=history(svc,a),history(svc,b)
        assert len({ha["chat_id"],hb["chat_id"],svc.identity.agent().chat_id})==3
        assert [m["content"] for m in ha["messages"]]==["部署 A","部署改分支"]
        assert [m["content"] for m in hb["messages"]]==["旅行 B","旅行换酒店"]
        assert history(svc,a)==ha
        with pytest.raises(LookupError):history(AgentContext(db,"other"),a)
        from core.db.repository.chat import ChatSessionRepository
        assert ChatSessionRepository(db).list_by_user("owner")==([],0)


def test_task_transcript_persists_normal_tools_and_thinking_without_main_chat_noise(sessions,monkeypatch):
    from core.db import engine
    monkeypatch.setattr(engine,"SessionLocal",sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    with sessions() as db:
        svc=AgentContext(db,"owner");a=svc.tasks.submit("检查文件","a")["id"]
        row=db.get(PersonalWorkItem,a);row.status="running";row.owner="worker";row.run_id="run";row.lease_until=time.time()+100
        db.add(ChatRun(run_id="run",chat_id=row.chat_id,user_id="owner",message_id="assistant-run",status="running"));db.commit()
    log=Transcript(a,"owner",SimpleNamespace(run_id="run"))
    log.record("thinking_delta","检查路径")
    log.record("tool_call",{"id":"tool","name":"pa_bash","args":{"command":"ls"}})
    log.record("tool_result",{"id":"tool","content":"file.txt","status":"success"})
    log.record("text_delta","检查完成");log.flush()
    with sessions() as db:
        svc=AgentContext(db,"owner");svc.tasks.complete(a,"检查完成",{})
        rows=history(svc,a)["messages"]
        assert len(rows)==2 and rows[-1]["thinking"]==[{"content":"检查路径"}]
        assert rows[-1]["tool_calls"][0]["result"]=="file.txt"
        # The result reaches the user through the agent's next turn, not as a raw message.
        assert [m["role"] for m in svc.conversation.list()]==["user"]
        assert not any("tool_calls" in m for m in svc.conversation.list())


def artifact(db,uid,chat,ident,mime,source="ai_generated",task=None):
    db.add(Artifact(artifact_id=ident,user_id=uid,chat_id=chat,type="document",title=ident,filename=ident,size_bytes=3,mime_type=mime,storage_key=ident,
        extra_data={"source":source,**({"personal_work_item_id":task} if task else {})}));db.commit()


def test_output_counts_cover_history_exclude_inputs_and_other_users(sessions):
    with sessions() as db:
        svc=AgentContext(db,"owner");a=svc.tasks.submit("生成文件","a")["id"]
        chat=db.get(PersonalWorkItem,a).chat_id
        artifact(db,"owner",chat,"doc","application/pdf",task=a)
        artifact(db,"owner",svc.identity.agent().chat_id,"image","image/png",task=a)
        artifact(db,"owner",chat,"input","image/png","user_upload")
        other=AgentContext(db,"other");other.identity.ensure();artifact(db,"other",other.identity.agent().chat_id,"private","image/png")
        page=listing(svc,limit=1)
        assert (page["total"],page["images"],page["documents"],page["has_more"])==(2,1,1,True)
        older=listing(svc,before=page["items"][-1]["id"],limit=1)
        assert older["total"]==2 and not older["has_more"]
        assert {page["items"][0]["id"],older["items"][0]["id"]}=={"doc","image"}
        with pytest.raises(LookupError):listing(svc,before="private")


def test_local_permission_is_off_by_default_and_revocation_rejects_commands(sessions):
    from personal_agent.computer.devices import pair, poll, consent, acknowledge
    with sessions() as db:
        svc=AgentContext(db,"owner");task=svc.tasks.submit("截图","a")["id"]
        row=db.get(PersonalWorkItem,task);row.status="running";row.run_id="run";row.lease_until=time.time()+100;db.commit()
        paired=pair(db,"owner","My desktop","windows",["observe"])
        db.add(PersonalDeviceCommand(id="cmd",device_id=paired["id"],epoch=1,work_item_id=task,run_id="run",action={"type":"observe"},deadline=time.time()+100));db.commit()
        assert poll(db,paired["id"],paired["token"]) is None
        with pytest.raises(LookupError):consent(db,paired["id"],"wrong token",True)
        consent(db,paired["id"],paired["token"],True)
        assert poll(db,paired["id"],paired["token"])["id"]=="cmd"
        consent(db,paired["id"],paired["token"],False)
        assert db.get(PersonalDeviceCommand,"cmd").status=="revoked"
        with pytest.raises(LookupError):acknowledge(db,paired["id"],paired["token"],"cmd",{"private_screenshot":"blocked"})


@pytest.mark.asyncio
async def test_human_control_is_exclusive_scoped_and_released(sessions,monkeypatch):
    from personal_agent.computer import human_control as human
    from personal_agent.computer import live_stream
    from core.infra import ephemeral
    state=ephemeral.LocalEphemeralState()
    for module in (human,live_stream):monkeypatch.setattr(module,"get_ephemeral_state",lambda:state)
    with sessions() as db:
        svc=AgentContext(db,"owner");svc.identity.ensure();agent=svc.identity.agent();agent.computer_status="ready";db.commit()
        token=(await human.begin(svc))["token"]
        assert not await state.hold("pa:desktop:"+agent.id,"agent-tool",ttl=45)
        with pytest.raises(ValueError):await human.begin(svc)
        assert (await live_stream.ticket(svc,token))["ticket"]
        assert await human.refresh(agent.id,"owner",token)
        other=AgentContext(db,"other");other.identity.ensure();other.identity.agent().computer_status="ready";db.commit()
        with pytest.raises(LookupError):await live_stream.ticket(other,token)
        await human.end(other,token)
        assert not await state.hold("pa:desktop:"+agent.id,"agent-tool",ttl=45)
        await human.end(svc,token)
        assert not await human.refresh(agent.id,"owner",token)
        with pytest.raises(LookupError):await live_stream.ticket(svc,token)
        assert await state.hold("pa:desktop:"+agent.id,"agent-tool",ttl=45)


def test_non_document_outputs_and_legacy_image_mime(sessions):
    with sessions() as db:
        svc=AgentContext(db,"owner");svc.identity.ensure()
        artifact(db,"owner",svc.identity.agent().chat_id,"archive.zip","application/zip")
        artifact(db,"owner",svc.identity.agent().chat_id,"legacy.png","application/octet-stream")
        page=listing(svc)
        assert (page["total"],page["images"],page["documents"],page["other"])==(2,1,0,1)
        assert next(row for row in page["items"] if row["id"]=="legacy.png")["mime_type"]=="image/png"


def test_late_clarification_projects_older_instruction_once(sessions):
    from personal_agent.models import PersonalMessageTask
    with sessions() as db:
        svc=AgentContext(db,"owner");task=svc.tasks.submit("部署","new")["id"]
        old=svc.conversation.append(svc.identity.agent(),"user","待澄清要求",None);db.commit()
        svc.tasks.steer(task,"较新要求","new-update")
        db.add(PersonalMessageTask(message_id=old.message_id,work_item_id=task,relation="update",instruction="澄清后补充旧要求"));db.commit()
        rows=history(svc,task)["messages"]
        assert [m["content"] for m in rows]==["部署","较新要求","澄清后补充旧要求"]
        assert history(svc,task)["messages"]==rows
