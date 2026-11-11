"""Source ids: what every fact line and every memory version points back to.

    conv-<message id>   a message of the agent's own (web) conversation
    chan-<message id>   a message of an IM channel conversation
    task-<task id>      a background task, its goal and result

The prefix carries trust: only the owner's own words in conv- and chan- sources may
establish preferences or decisions (see _schema.md and the Dreaming prompts).
"""
import re

SOURCE_ID = re.compile(r"^(conv|chan|task)-([A-Za-z0-9_.:-]{1,120})$")


def parse(sid):
    """(kind, ident) or None when the text is not a source id."""
    match = SOURCE_ID.match(sid or "")
    return (match[1], match[2]) if match else None


def for_message(agent, message):
    """The source id of a chat message: conv- in the agent's own chat, chan- elsewhere."""
    kind = "conv" if message.chat_id == agent.chat_id else "chan"
    return kind + "-" + message.message_id


def for_task(task):
    return "task-" + task.id


def exists(db, agent, sid):
    """True when the source is a message or task of this agent."""
    parsed = parse(sid)
    if not parsed: return False
    kind, ident = parsed
    if kind == "task":
        from personal_agent.models import PersonalWorkItem
        task = db.get(PersonalWorkItem, ident)
        return task is not None and task.agent_id == agent.id
    from core.db.models import ChatMessage
    from personal_agent.conversation.conversations import require_session
    message = db.get(ChatMessage, ident)
    if message is None or (kind == "conv") != (message.chat_id == agent.chat_id): return False
    try: require_session(db, agent, message.chat_id)
    except LookupError: return False
    return True


def checker(db, agent):
    """A cached exists() for lint over many lines."""
    known = {}

    def check(sid):
        if sid not in known: known[sid] = exists(db, agent, sid)
        return known[sid]
    return check
