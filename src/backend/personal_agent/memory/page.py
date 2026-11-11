"""Reading the page format of _schema.md: frontmatter, fact lines, links, stubs.

A page:

    ---
    name: project-q4-budget
    description: 是什么、什么时候该读
    aliases: ["Q4 预算"]
    type: project
    status: active
    updated: 2026-11-09
    ---
    ## 金额
    - 云资源 55 万；was 64 万 until 2026-11-08 [src: conv-pa_msg_1 | as-of: 2026-11-08]

Parsing is deliberately small and line based: lint and the Dreaming host steps read it,
the models write it.
"""
import json
import re
from dataclasses import dataclass, field

FIELDS = ("name", "description", "aliases", "type", "status", "updated")
SOURCE_TAG = re.compile(r"\[src:\s*([^\]|]+?)\s*\|\s*as-of:\s*(\d{4}-\d{2}-\d{2})\s*\]")
LINK = re.compile(r"\[\[([a-z0-9-]+)\]\]")
STUB = re.compile(r"^\s*MOVED TO \[\[([a-z0-9-]+)\]\]\s*$")
CURRENT_PICTURE = "## Current picture"
LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")


@dataclass
class Page:
    meta: dict = field(default_factory=dict)
    body: str = ""
    has_frontmatter: bool = False

    @property
    def aliases(self):
        value = self.meta.get("aliases")
        return [a for a in value if isinstance(a, str)] if isinstance(value, list) else []

    def fact_lines(self):
        """List items under a ## heading (-, *, 1., indented too): each one fact with a source tag.

        Only a Current picture section that opens the body is a summary without tags."""
        heading, first, out = None, None, []
        for line in self.body.splitlines():
            if line.startswith("## "):
                heading = line.strip()
                first = heading if first is None else first
                continue
            if heading and not (heading == CURRENT_PICTURE and heading == first) and LIST_ITEM.match(line):
                out.append(line)
        return out


def parse(text):
    if not text.startswith("---\n"):
        return Page(body=text)
    end = text.find("\n---", 4)
    if end < 0:
        return Page(body=text)
    meta = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if not sep: continue
        key, value = key.strip(), value.strip()
        if value.startswith("["):
            try: value = json.loads(value)
            except ValueError: pass
        meta[key] = value
    rest = text[end + 4:]
    return Page(meta=meta, body=rest[1:] if rest.startswith("\n") else rest, has_frontmatter=True)


def sources(text):
    """Every source id cited in the text, in order of first appearance."""
    seen = []
    for match in SOURCE_TAG.finditer(text):
        if match[1] not in seen: seen.append(match[1])
    return seen


def links(text):
    return sorted(set(LINK.findall(text)))


def stub_target(text):
    """The slug a one-line MOVED TO stub points at, or None."""
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if len(lines) != 1: return None
    match = STUB.match(lines[0])
    return match[1] if match else None


def has_current_picture(text):
    """The body opens with a Current picture section."""
    headings = [line.strip() for line in parse(text).body.splitlines() if line.startswith("## ")]
    return bool(headings) and headings[0] == CURRENT_PICTURE


def section(text, heading):
    """The lines of one ## section, heading included, or "" when it is absent."""
    lines, out, inside = text.splitlines(), [], False
    for line in lines:
        if line.startswith("## "):
            if inside: break
            inside = line.strip() == heading
        if inside: out.append(line)
    return "\n".join(out)
