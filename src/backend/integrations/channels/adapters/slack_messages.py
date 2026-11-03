"""Slack message text helpers: inbound mrkdwn → plain text, outbound Markdown → mrkdwn.

Slack does not render CommonMark. Its ``mrkdwn`` uses ``*bold*`` / ``_italic_`` /
``~strike~`` / ``<url|label>`` links, has no headings or tables, and requires ``&``, ``<``
and ``>`` to be entity-escaped (they delimit links and mentions). Agent replies are
CommonMark, so they are converted here before sending; code spans and fences are left
alone apart from the mandatory escaping.

Inbound text arrives in the same encoding: mentions as ``<@U123>``, links as
``<https://x|label>``, entities escaped. It is turned back into readable plain text.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

# <@U123> / <@W123|name>: user (incl. bot user) mentions
_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
_LEADING_MENTIONS = re.compile(r"^(?:\s*<@[UW][A-Z0-9]+(?:\|[^>]*)?>)+\s*")
# <#C123|general>, <!here>, <https://x|label>, <mailto:a@b|a@b>
_CHANNEL_REF = re.compile(r"<#[A-Z0-9]+\|([^>]*)>")
_SPECIAL = re.compile(r"<!(here|channel|everyone)(?:\|[^>]*)?>")
_LINK = re.compile(r"<((?:https?|mailto):[^|>]+)(?:\|([^>]*))?>")

# Message subtypes that are user-authored content; every other subtype (joins, edits,
# deletions, bot messages, topic changes…) is ignored by the inbound parser.
CONTENT_SUBTYPES = (None, "", "file_share", "thread_broadcast")


def mentioned_ids(text: str) -> List[str]:
    return _MENTION.findall(text or "")


def inbound_text(text: str) -> str:
    """Slack-encoded message text → plain text, with the leading @bot mention(s) removed."""
    text = _LEADING_MENTIONS.sub("", text or "")
    text = _MENTION.sub(lambda m: f"@{m.group(1)}", text)
    text = _CHANNEL_REF.sub(lambda m: f"#{m.group(1)}", text)
    text = _SPECIAL.sub(lambda m: f"@{m.group(1)}", text)
    text = _LINK.sub(lambda m: m.group(1).removeprefix("mailto:") if not m.group(2) else f"{m.group(2)} ({m.group(1)})", text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").strip()


def file_attachments(files: Any) -> List[Dict[str, Any]]:
    """Slack ``files`` array → normalized attachments (key = private download URL)."""
    out: List[Dict[str, Any]] = []
    for f in files or []:
        if not isinstance(f, dict):
            continue
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue  # external / tombstoned files carry no downloadable body
        kind = "image" if str(f.get("mimetype") or "").startswith("image/") else "file"
        out.append({"kind": kind, "key": url, "name": f.get("name") or f.get("id") or "file"})
    return out


# ── Outbound: CommonMark → mrkdwn ──────────────────────────────────────────
_FENCE = re.compile(r"(```.*?```)", re.S)
_INLINE_CODE = re.compile(r"(`[^`\n]+`)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_ITALIC = re.compile(r"(?<![*\w])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![*\w])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.M)
_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.M)
_BOLD_MARK = "\x01"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _is_table_line(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and s.count("|") >= 2


def _fence_tables(text: str) -> str:
    """Slack has no tables: keep them legible by putting each table in a code block."""
    lines, out, block = text.split("\n"), [], []
    for line in lines + [""]:
        if _is_table_line(line):
            block.append(line)
            continue
        if len(block) >= 2:
            out.extend(["```", *block, "```"])
        else:
            out.extend(block)
        block = []
        out.append(line)
    return "\n".join(out[:-1])


def _convert_prose(text: str) -> str:
    text = _escape(text)
    text = _MD_LINK.sub(lambda m: f"<{m.group(2)}|{m.group(1)}>", text)
    text = _HEADING.sub(lambda m: f"{_BOLD_MARK}{m.group(1)}{_BOLD_MARK}", text)
    text = _BOLD.sub(lambda m: f"{_BOLD_MARK}{m.group(2)}{_BOLD_MARK}", text)
    text = _ITALIC.sub(lambda m: f"_{m.group(1)}_", text)
    text = _STRIKE.sub(lambda m: f"~{m.group(1)}~", text)
    text = _BULLET.sub(lambda m: f"{m.group(1)}• ", text)
    return text.replace(_BOLD_MARK, "*")


def markdown_to_mrkdwn(text: str) -> str:
    parts: List[str] = []
    for i, block in enumerate(_FENCE.split(_fence_tables(text or ""))):
        if i % 2:  # fenced code: drop the language tag Slack would print literally
            body = re.sub(r"^```[^\n`]*\n", "```\n", block, count=1)
            parts.append(_escape(body))
            continue
        for j, seg in enumerate(_INLINE_CODE.split(block)):
            parts.append(_escape(seg) if j % 2 else _convert_prose(seg))
    return "".join(parts)
