"""Worker sessions, their history projection and the stream record the agent's own turn uses."""
import time
import uuid
from sqlalchemy import select
from core.db.models import ChatSession, ChatMessage
from core.db.repository.chat import ChatMessageRepository

def append(db, session, role, content, metadata=None, ident=None):
    row=ChatMessageRepository(db).create({"message_id":ident or "pa_msg_"+uuid.uuid4().hex,
        "chat_id":session.chat_id,"role":role,"content":content[:100000],"extra_data":metadata or {}},commit=False)
    session.message_count+=1
    return row

def sync_thread(db, task, uid=None):
    """The task's Worker session: its persistent model history (SUPERAGENT_DESIGN §8.1).

    Created on first use. Updates and results are not copied into it: the executor
    hands updates to the Worker (worker/inputs.py) and the Worker's runs write the rest."""
    from personal_agent.models import PersonalAgent
    ident=task.chat_id or "pa_chat_task_"+task.id.removeprefix("pa_task_")
    owner=db.get(PersonalAgent,task.agent_id).user_id
    if uid is not None and uid!=owner:raise PermissionError("Task session belongs to another owner")
    session=db.get(ChatSession,ident)
    if session is None:
        session=ChatSession(chat_id=ident,user_id=owner,title=task.goal[:200],extra_data={"personal_agent_task":task.id})
        db.add(session);db.flush()
    if session.user_id!=owner:raise PermissionError("Task session belongs to another owner")
    task.chat_id=ident
    return session

def legacy(db, task):
    """A task run by the earlier task loop: it has no Assignment and has run (or ended)."""
    from personal_agent.models import PersonalAssignment
    return task.status not in ("queued", "computer_wait") and db.scalar(select(PersonalAssignment.id).where(
        PersonalAssignment.work_item_id == task.id).limit(1)) is None


def import_legacy(db, task, session):
    """The earlier loop mirrored the goal, updates and final reply into the session; finish that copy."""
    from sqlalchemy import exists
    from sqlalchemy.orm import aliased
    from personal_agent.models import PersonalMessageTask
    ident=session.chat_id
    if not db.scalar(select(ChatMessage.message_id).where(ChatMessage.chat_id==ident).limit(1)):
        append(db,session,"user",task.goal,{"task_goal":True})
    projected=aliased(ChatMessage)
    imported=exists(select(projected.message_id).where(projected.chat_id==ident,
        projected.extra_data["source_message_id"].as_string()==ChatMessage.message_id))
    rows=db.execute(select(ChatMessage,PersonalMessageTask).join(PersonalMessageTask,
        PersonalMessageTask.message_id==ChatMessage.message_id).where(PersonalMessageTask.work_item_id==task.id,
        ChatMessage.chat_id!=ident,PersonalMessageTask.relation.in_(["update","final"]),~imported).order_by(ChatMessage.created_at,ChatMessage.message_id)).all()
    for message,link in rows:
        if link.relation=="update":append(db,session,"user",link.instruction,{"source_message_id":message.message_id})
        elif link.relation=="final":
            recorded=db.scalar(select(ChatMessage.message_id).where(ChatMessage.chat_id==ident,
                ChatMessage.extra_data["run_id"].as_string()==task.run_id)) if task.run_id else None
            if not recorded:append(db,session,"assistant",link.instruction,{"source_message_id":message.message_id})


def terminal(db, task, shown, old):
    """The reply that answers the task now; an older reply must not clear a newer completion reminder."""
    from personal_agent.models import PersonalAssignment
    if not old:
        latest=db.scalar(select(PersonalAssignment).where(PersonalAssignment.work_item_id==task.id)
                         .order_by(PersonalAssignment.created_at.desc()).limit(1))
        return next((m.message_id for m in reversed(shown) if m.role=="assistant" and m.content.strip()
                     and latest is not None and m.created_at>=latest.created_at),None)
    for message in reversed(shown):
        metadata=message.extra_data or {}
        if message.role!="assistant" or not message.content.strip():continue
        if task.run_id and metadata.get("run_id")==task.run_id:return message.message_id
        source=metadata.get("source_message_id")
        if source:
            original=db.get(ChatMessage,source)
            if original and original.created_at>=task.updated_at:return message.message_id
    return None


def history(svc, ident, before=None, limit=100):
    task=svc.tasks.get(ident,lock=True);session=sync_thread(svc.db,task,svc.user_id)
    old=legacy(svc.db,task)
    if old:import_legacy(svc.db,task,session)
    svc.db.commit()
    query=select(ChatMessage).where(ChatMessage.chat_id==session.chat_id)
    if before is not None:query=query.where(ChatMessage.chat_seq<before)
    rows=list(svc.db.scalars(query.order_by(ChatMessage.chat_seq.desc()).limit(limit)));rows.reverse()
    # The Worker's briefs, notes and wake-ups are model context, not conversation.
    shown=[m for m in rows if not (m.extra_data or {}).get("hidden_in_chat")]
    from core.chat.inflight import marker
    from core.chat.sequencer import ChatSequencer
    active=ChatSequencer(svc.db).get_active_run(session.chat_id)
    live=active.run_id if active and active.status in ("pending","running") else None
    terminal_message_id=terminal(svc.db,task,shown,old) if task.status in {"completed","failed"} and live is None else None
    return {"chat_id":session.chat_id,"task_id":ident,"task_run_id":task.run_id,
        "task_status":task.status,"terminal_message_id":terminal_message_id,"has_more":len(rows)==limit,"messages":[
        {"message_id":m.message_id,"chat_id":m.chat_id,"chat_seq":m.chat_seq,"role":m.role,"content":m.content,
         "thinking":m.thinking,"tool_calls":m.tool_calls,"metadata":m.extra_data or {},
         "created_at":m.created_at.isoformat(),"is_streaming":bool(live) and (marker(m.extra_data) or {}).get("run_id")==live}
        for m in shown]}

class StreamRecord:
    """One agent run as the chat history stores it: display segments, thinking and tool calls.

    Canonical model steps are stored separately from display fields."""
    def __init__(self):
        self.text="";self.thinking=[];self.tools=[];self.segments=[];self.last_flush=0;self.model_steps=[]

    def take(self,event,payload):
        """Add one stream event. True when the record should be written now."""
        if event=="model_step":
            self.model_steps.append(payload)
        elif event=="text_delta":
            self.text=(self.text+str(payload))[-100000:]
            if self.segments and self.segments[-1]["type"]=="text":self.segments[-1]["text"]=(self.segments[-1]["text"]+str(payload))[-100000:]
            else:self.segments.append({"type":"text","text":str(payload)})
        elif event=="thinking_delta":
            if not self.segments or self.segments[-1]["type"]!="thinking":
                self.thinking.append({"content":""});self.segments.append({"type":"thinking","index":len(self.thinking)-1})
            self.thinking[-1]["content"]=(self.thinking[-1]["content"]+str(payload))[-50000:]
        elif event=="tool_call":
            self.tools.append({"tool_id":payload["id"],"tool_name":payload["name"],"tool_args":payload.get("args"),"status":"running","started_at":time.time()*1000})
            self.segments.append({"type":"tool","index":len(self.tools)-1});self.text=""
        elif event=="tool_result":
            for tool in self.tools:
                if tool["tool_id"]==payload["id"]:
                    tool.update(result=str(payload.get("content",""))[:50000],status="error" if payload.get("status") in {"error","denied","interrupted"} else "success")
        return event in {"tool_call","tool_result","model_step"} or time.monotonic()-self.last_flush>1

    def store(self,row,content):
        row.model_steps=list(self.model_steps)
        row.content=content;row.thinking=list(self.thinking);row.tool_calls=list(self.tools)
        row.extra_data={
            **(row.extra_data or {}), "segments":self.segments[-300:],
            "model_history_display_only": not bool(self.model_steps),
        }
        from core.llm.context.safety import remember_context_output

        remember_context_output(
            message_id=row.message_id, steps=row.model_steps or [], content=row.content,
        )
        self.last_flush=time.monotonic()
