"""Database-paged search over user-visible chat text.

Legacy thinking is excluded before LIMIT, including conversations with more than
20 reasoning-only matches. SQLite uses the same deterministic text projection;
PostgreSQL evaluates its equivalent in SQL. No ORM messages/results are loaded
until after the authorized page has been selected.
"""

from sqlalchemy import Text, and_, case, func, or_, select, null
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement
from core.db.models import ChatMessage, ChatSession


def _visible_message_text(content: str) -> str:
    """assistant 消息 content 的可见正文（剥离 <think> 思考段）。

    存储格式（多轮工具调用）：``思考1</think>可见文本<think>思考2</think>…最终正文``；
    也可能只有裸 ``</think>`` 分隔（开标签被服务栈吞掉）。规则与前端
    ``utils/segments.ts`` 的历史重建一致：每个 ``</think>`` 之前、上一个
    ``<think>`` 之后的内容是思考；其余是可见正文。
    """
    if "</think>" not in content and "<think>" not in content:
        return content
    parts = content.split("</think>")
    out: list[str] = []
    for i, part in enumerate(parts):
        if i == len(parts) - 1:
            # 最后一段：若有未闭合的 <think>，其后是（被截断的）思考
            out.append(part.split("<think>", 1)[0])
        else:
            idx = part.find("<think>")
            if idx >= 0:
                out.append(part[:idx])
            # 无开标签 → 整段是思考，丢弃
    return " ".join(x for x in out if x.strip()).strip()


class VisibleMessageText(FunctionElement):
    type = Text()
    inherit_cache = True


@compiles(VisibleMessageText, "sqlite")
def _sqlite_visible(element, compiler, **kw):
    return "jx_visible_text(" + compiler.process(list(element.clauses)[0], **kw) + ")"


@compiles(VisibleMessageText, "postgresql")
def _postgres_visible(element, compiler, **kw):
    value = compiler.process(list(element.clauses)[0], **kw)
    # Same split/join as _visible_message_text, preserving all visible sections.
    return f"""(CASE WHEN strpos({value}, '<think>') = 0 AND strpos({value}, '</think>') = 0
      THEN {value} ELSE (SELECT btrim(coalesce(string_agg(piece, ' ' ORDER BY ord), '')) FROM (
        SELECT ord, CASE WHEN ord = cardinality(string_to_array({value}, '</think>'))
          OR strpos(part, '<think>') > 0 THEN split_part(part, '<think>', 1) ELSE '' END AS piece
        FROM unnest(string_to_array({value}, '</think>')) WITH ORDINALITY AS parts(part, ord)
      ) AS visible_parts WHERE btrim(piece) <> '') END)"""


def _snippet(content, query):
    content = (content or "").replace("\n", " ")
    at = content.lower().find(query.lower())
    start = max(0, at - 15)
    end = min(len(content), start + 30)
    return ("..." if start else "") + content[start:end] + ("..." if end < len(content) else "")


def search_sessions(db, user_id, query, page=1, page_size=20, scope="title"):
    if db.get_bind().dialect.name == "sqlite":
        db.connection().connection.driver_connection.create_function(
            "jx_visible_text",
            1,
            lambda text: _visible_message_text(text or ""),
            deterministic=True,
        )
    # Treat user input literally (especially % and _) consistently in title/body.
    pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    title_match = ChatSession.title.ilike(pattern, escape="\\")
    visible = VisibleMessageText(ChatMessage.content)
    content_match = and_(
        ChatMessage.chat_id == ChatSession.chat_id,
        ChatMessage.role.in_(("user", "assistant")),
        ChatMessage.content.ilike(pattern, escape="\\"),
        visible.ilike(pattern, escape="\\"),
    )
    exists = select(1).where(content_match).correlate(ChatSession).exists()
    eligible = db.query(ChatSession).filter(
        ChatSession.user_id == user_id,
        ChatSession.deleted_at.is_(None),
        or_(title_match, exists) if scope == "all" else title_match,
    )
    total = eligible.count()
    first_content = (
        select(visible)
        .where(content_match)
        .order_by(ChatMessage.chat_seq)
        .limit(1)
        .correlate(ChatSession)
        .scalar_subquery()
    )
    body = case((title_match, None), else_=first_content) if scope == "all" else None
    rows = (
        eligible.add_columns(
            title_match.label("title_match"),
            (body if body is not None else null()).label("snippet_source"),
        )
        .order_by(title_match.desc(), ChatSession.updated_at.desc(), ChatSession.chat_id)
        .offset((max(1, page) - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return [
        dict(
            session=session,
            match_type="title" if is_title else "content",
            matched_snippet=None if is_title else _snippet(content, query),
        )
        for session, is_title, content in rows
    ], total
