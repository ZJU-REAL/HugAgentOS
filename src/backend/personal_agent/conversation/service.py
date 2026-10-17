"""Owner-scoped conversation operations."""
from sqlalchemy import select

import uuid

from core.db.models import ChatSession, ChatMessage, Artifact
from core.db.repository.chat import ChatMessageRepository
from personal_agent.models import PersonalWorkItem, PersonalMessageTask

class ConversationService:
    def __init__(self, context):
        self.context = context
        self.db, self.user_id = context.db, context.user_id

    @property
    def chat_id(self):
        return self.context.chat_id

    def id(self):
        from personal_agent.conversation.conversations import require_session
        return require_session(self.db, self.context.identity.agent(), self.chat_id).chat_id

    def append(self, agent, role, text, task_id, request_key=None, relation=None, reply_to=None, message_id=None):
        if len(text) > 100000: raise ValueError("Message exceeds 100000 characters")
        from personal_agent.conversation.conversations import destination
        task = self.db.get(PersonalWorkItem, task_id) if task_id else None
        chat_id = destination(self.db, agent, selected=self.chat_id, task=task, reply_to=reply_to)
        message = ChatMessageRepository(self.db).create({"message_id": message_id or "pa_msg_" + uuid.uuid4().hex,
            "chat_id": chat_id, "role": role, "content": text,
            "extra_data": {"work_item_id": task_id, "request_key": request_key}}, commit=False)
        if task_id and self.db.get(PersonalWorkItem, task_id):
            self.db.add(PersonalMessageTask(message_id=message.message_id, work_item_id=task_id,
                relation=relation or ("final" if role == "assistant" else "update"), instruction=text))
        if role == "assistant":
            # Replies to an IM message, or for a task started in IM, return to that conversation.
            # Unattended (scheduled or heartbeat) work has no IM message; it goes to the home bot.
            from personal_agent.channels.channel_bridge import mark_for_delivery
            from personal_agent.scheduling.schedules import is_unattended_run
            task = self.db.get(PersonalWorkItem, task_id) if task_id else None
            mark_for_delivery(self.db, message, task.id if task else None, reply_to, home=is_unattended_run(task))
        session = self.db.get(ChatSession, chat_id)
        session.message_count += 1
        from core.infra.time import utc_now
        session.last_message_at = utc_now()
        if task_id and self.db.get(PersonalWorkItem, task_id):
            self.db.flush()
            from personal_agent.tasks.task_threads import sync_thread
            sync_thread(self.db, self.db.get(PersonalWorkItem, task_id), self.user_id)
        return message

    def list(self, after_seq=0, limit=100, before_seq=None):
        agent = self.context.identity.agent()
        if agent is None:
            if self.chat_id: raise LookupError("Conversation not found")
            return []
        query = select(ChatMessage).where(ChatMessage.chat_id == self.id(), ChatMessage.role.in_(["user", "assistant"]))
        descending = before_seq is not None or after_seq == 0
        if before_seq is not None: query = query.where(ChatMessage.chat_seq < before_seq)
        elif after_seq: query = query.where(ChatMessage.chat_seq > after_seq)
        query = query.order_by(ChatMessage.chat_seq.desc() if descending else ChatMessage.chat_seq).limit(limit)
        rows = list(self.db.scalars(query))
        if descending: rows.reverse()
        # A heartbeat check in progress usually ends quiet: its reply is shown only when finished.
        rows = [row for row in rows if not ((row.extra_data or {}).get("streaming") and (row.extra_data or {}).get("heartbeat_turn"))]
        from personal_agent.conversation.associations import message_links
        from personal_agent.delivery.delivery import artifacts_for_message
        links = message_links(self.db, [r.message_id for r in rows])
        return [{"id": row.message_id, "seq": row.chat_seq, "role": row.role, "content": row.content,
                 "origin": (row.extra_data or {}).get("origin"),
                 "channel_type": ((row.extra_data or {}).get("channel_origin") or {}).get("channel_type"),
                 "work_item_id": (row.extra_data or {}).get("work_item_id"),
                 "work_item_ids": list(dict.fromkeys(link["work_item_id"] for link in links[row.message_id])),
                 "task_links": links[row.message_id],
                 "reply_to": (row.extra_data or {}).get("reply_to"),
                 "heartbeat": bool((row.extra_data or {}).get("heartbeat")),
                 "streaming": bool((row.extra_data or {}).get("streaming")),
                 "activity": (row.extra_data or {}).get("activity"),
                 "artifacts": artifacts_for_message(self.db, row, self.user_id),
                 "attachments": [{"id": a.artifact_id, "name": a.filename, "url": "/api/files/" + a.artifact_id}
                    for ident in (row.extra_data or {}).get("artifact_ids", [])
                    if not (row.extra_data or {}).get("delivery") and (a := self.db.get(Artifact, ident)) and a.user_id == self.user_id]} for row in rows]
