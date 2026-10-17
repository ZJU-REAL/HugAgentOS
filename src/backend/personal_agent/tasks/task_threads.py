"""Private task sessions and the ordinary chat history projection."""
import time
import uuid
from sqlalchemy import select,exists
from sqlalchemy.orm import aliased
from core.db.models import ChatSession, ChatMessage, ChatRun
from core.db.repository.chat import ChatMessageRepository
from personal_agent.models import PersonalMessageTask

def append(db, session, role, content, metadata=None, ident=None):
    row=ChatMessageRepository(db).create({"message_id":ident or "pa_msg_"+uuid.uuid4().hex,
        "chat_id":session.chat_id,"role":role,"content":content[:100000],"extra_data":metadata or {}},commit=False)
    session.message_count+=1
    return row

def sync_thread(db, task, uid):
    ident=task.chat_id or "pa_chat_task_"+task.id.removeprefix("pa_task_")
    session=db.get(ChatSession,ident)
    if session is None:
        session=ChatSession(chat_id=ident,user_id=uid,title=task.goal[:200],extra_data={"personal_agent_task":task.id})
        db.add(session);db.flush();append(db,session,"user",task.goal,{"task_goal":True})
    if session.user_id!=uid:raise PermissionError("Task session belongs to another owner")
    task.chat_id=ident
    projected=aliased(ChatMessage)
    imported=exists(select(projected.message_id).where(projected.chat_id==ident,
        projected.extra_data["source_message_id"].as_string()==ChatMessage.message_id))
    # A clarification can attach an older message after later task updates were imported.
    unread=~imported
    rows=db.execute(select(ChatMessage,PersonalMessageTask).join(PersonalMessageTask,
        PersonalMessageTask.message_id==ChatMessage.message_id).where(PersonalMessageTask.work_item_id==task.id,
        ChatMessage.chat_id!=ident,PersonalMessageTask.relation.in_(["update","final"]),unread).order_by(ChatMessage.created_at,ChatMessage.message_id)).all()
    for message,link in rows:
        if link.relation=="update":append(db,session,"user",link.instruction,{"source_message_id":message.message_id})
        elif link.relation=="final":
            recorded=db.scalar(select(ChatMessage.message_id).where(ChatMessage.chat_id==ident,
                ChatMessage.extra_data["run_id"].as_string()==task.run_id)) if task.run_id else None
            if not recorded:append(db,session,"assistant",link.instruction,{"source_message_id":message.message_id})
    return session

def history(svc, ident, before=None, limit=100):
    task=svc.tasks.get(ident,lock=True);session=sync_thread(svc.db,task,svc.user_id);svc.db.commit()
    query=select(ChatMessage).where(ChatMessage.chat_id==session.chat_id)
    if before is not None:query=query.where(ChatMessage.chat_seq<before)
    rows=list(svc.db.scalars(query.order_by(ChatMessage.chat_seq.desc()).limit(limit)));rows.reverse()
    # The dialog acknowledges only the terminal response from this exact run.
    # Older assistant messages must not clear a newly completed task reminder.
    terminal_message_id = None
    if task.status in {"completed", "failed"}:
        for message in reversed(rows):
            metadata = message.extra_data or {}
            if message.role != "assistant" or not message.content.strip():
                continue
            if task.run_id and metadata.get("run_id") == task.run_id:
                terminal_message_id = message.message_id
                break
            source = metadata.get("source_message_id")
            if source:
                original = svc.db.get(ChatMessage, source)
                if original and original.created_at >= task.updated_at:
                    terminal_message_id = message.message_id
                    break
    return {"chat_id":session.chat_id,"task_id":ident,"task_revision":task.revision,
        "task_status":task.status,"terminal_message_id":terminal_message_id,"has_more":len(rows)==limit,"messages":[
        {"message_id":m.message_id,"chat_id":m.chat_id,"chat_seq":m.chat_seq,"role":m.role,"content":m.content,
         "thinking":m.thinking,"tool_calls":m.tool_calls,"metadata":m.extra_data or {},
         "created_at":m.created_at.isoformat(),"is_streaming":task.status=="running" and (m.extra_data or {}).get("run_id")==task.run_id}
        for m in rows]}

class StreamRecord:
    """One agent run as the chat history stores it: display segments, thinking and tool calls.

    The same record serves the page and model replay (core.llm.model_steps.assistant_row_replay)."""
    def __init__(self):
        self.text="";self.thinking=[];self.tools=[];self.segments=[];self.last_flush=0

    def take(self,event,payload):
        """Add one stream event. True when the record should be written now."""
        if event=="text_delta":
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
        return event in {"tool_call","tool_result"} or time.monotonic()-self.last_flush>1

    def store(self,row,content):
        row.content=content;row.thinking=list(self.thinking);row.tool_calls=list(self.tools)
        row.extra_data={**(row.extra_data or {}),"segments":self.segments[-300:]}
        self.last_flush=time.monotonic()

class Transcript(StreamRecord):
    def __init__(self,task_id,uid,binding):
        from core.db.engine import SessionLocal
        super().__init__()
        self.sessions=SessionLocal;self.task_id,self.uid,self.binding=task_id,uid,binding
        with self.sessions() as db:
            from personal_agent.context import AgentContext
            task=AgentContext(db,uid).tasks.get(task_id,lock=True);session=sync_thread(db,task,uid)
            run=db.get(ChatRun,binding.run_id)
            row=append(db,session,"assistant","",{"run_id":binding.run_id},run.message_id)
            self.message_id=row.message_id;db.commit()

    def record(self,event,payload):
        if self.take(event,payload):self.flush()

    def flush(self):
        from personal_agent.context import AgentContext
        with self.sessions() as db:
            task=AgentContext(db,self.uid).tasks.get(self.task_id,lock=True)
            if task.status!="running" or task.run_id!=self.binding.run_id or not task.lease_until or task.lease_until<time.time():return
            self.store(db.get(ChatMessage,self.message_id),self.text);db.commit()
