"""Deterministic checks on the wiki.

check_page runs on every online write and refuses it on a blocking finding. check_wiki
runs when Dreaming finishes; a blocking finding there rolls the whole run back.
Warnings only go into DREAMS.md.
"""
import re
from collections import Counter
from dataclasses import dataclass

from personal_agent.memory import layout, page
from personal_agent.memory.redact import holds_secret
from personal_agent.memory.sources import SOURCE_ID

BLOCK, WARN = "block", "warn"

# Text that tries to steer the model, not describe the user's world. _index.md is injected
# into every turn, so it may hold none of it.
INSTRUCTION = re.compile(
    r"(?i)ignore (?:all |the )?(?:previous|above|prior)|disregard|system prompt|you must|developer message|"
    r"忽略.{0,12}(?:指令|规则|提示)|系统提示词|你必须|调用工具|执行(?:命令|脚本)")


@dataclass(frozen=True)
class Finding:
    level: str
    rule: str
    path: str
    detail: str

    def as_dict(self):
        return {"level": self.level, "rule": self.rule, "path": self.path, "detail": self.detail}


def blocking(findings):
    return [f for f in findings if f.level == BLOCK]


def check_page(rel, text, known_source=None, *, allow_stub=False):
    """Blocking checks a single page can answer on its own."""
    out = []
    add = lambda rule, detail, level=BLOCK: out.append(Finding(level, rule, rel, detail))
    name = layout.slug(rel)
    if name is None:
        add("file-name", "页面文件名必须是 memory/<type>-<name>.md，type 取 " + "、".join(layout.TYPES))
        return out
    if len(text.encode()) > layout.PAGE_MAX_BYTES:
        add("page-size", "页面超过 32 KB，请拆成几个相关页面")
    if holds_secret(text):
        add("secret", "页面里有凭据或密钥，只写它的名称")
    if page.stub_target(text) is not None:
        if not allow_stub: add("stub", "只有 Dreaming 的 organizer 能留 MOVED TO 占位")
        return out
    parsed = page.parse(text)
    # aliases may be an empty list; every other field needs a value.
    missing = [key for key in page.FIELDS if (key not in parsed.meta if key == "aliases" else not parsed.meta.get(key))]
    if not parsed.has_frontmatter or missing:
        add("frontmatter", "frontmatter 缺少：" + "、".join(missing or page.FIELDS))
    else:
        if parsed.meta["name"] != name: add("frontmatter", "name 必须等于文件名 " + name)
        if parsed.meta["type"] != name.split("-", 1)[0]: add("frontmatter", "type 必须与文件名前缀一致")
        if parsed.meta["status"] not in layout.STATUSES: add("frontmatter", "status 只能是 active 或 archived")
    for line in parsed.fact_lines():
        if _item(line).startswith("CONFLICT:"):
            if not page.SOURCE_TAG.search(line) and not _cited(line, known_source): add("fact-source", "冲突行要写明两边的来源：" + line[:80])
            continue
        if not page.SOURCE_TAG.search(line):
            add("fact-source", "事实行缺少 [src: … | as-of: …]：" + line[:80])
            continue
        if known_source:
            for sid in page.sources(line):
                if not known_source(sid): add("fact-source", "来源不存在：" + sid)
    return out


def _item(line):
    return re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line)


def check_new_facts(rel, before, text, source_id):
    """An online write cites what this turn or task said: every fact line it adds or rewrites
    names source_id. Lines it keeps as they were keep their old sources."""
    old = set(page.parse(before or "").fact_lines())
    return [Finding(BLOCK, "fact-source", rel, "新写或改写的事实行要标这一轮的来源 " + source_id + "：" + line.strip()[:80])
            for line in page.parse(text).fact_lines() if line not in old and source_id not in page.sources(line) + _inline(line)]


def _repeated(text):
    """Tagged fact lines that occur more than once; an untagged "- 无" may repeat across sections."""
    lines = [line.strip() for line in page.parse(text or "").fact_lines() if page.sources(line)]
    return {line: n for line, n in Counter(lines).items() if n > 1}


def check_new_duplicates(rel, before, text):
    """A model write may not add a second copy of a fact line the page holds. Seen live: the
    organizer pasted a paragraph into a page that had it, then removed and re-added it until
    its step cap. A copy the page already had is left for the organizer (a warning)."""
    old = _repeated(before)
    return [Finding(BLOCK, "duplicate-fact", rel, "这一行在页面里已经有了，只保留一处：" + line.strip()[:80])
            for line, n in _repeated(text).items() if n > old.get(line, 1)]


def check_new_citations(rel, text, allowed):
    """Dreaming may cite the source it analyzes or a source the wiki already cites: no new
    source id appears unless it is the one being read."""
    return [Finding(BLOCK, "fact-source", rel, "引用了这次没有读、wiki 里也没有的来源：" + sid)
            for sid in page.sources(text) if sid not in allowed]


def check_note(rel, text):
    """USER.md and MEMORY.md are injected into every turn: no credentials."""
    return [Finding(BLOCK, "secret", rel, "里面有凭据或密钥，只写它的名称")] if holds_secret(text) else []


def _inline(line):
    return [word for word in re.split(r"[\s（）()，,；;：:]+", line) if SOURCE_ID.match(word)]


def _cited(line, known_source):
    """A CONFLICT line may name its sources inline instead of in a tag."""
    ids = _inline(line)
    return bool(ids) and all(known_source(sid) for sid in ids) if known_source else bool(ids)


def check_wiki(files, schema_text, known_source=None, *, new_pages=(), source_bytes=None):
    """Every check over the whole wiki. files: {rel: text} for everything under memory/."""
    out = []
    pages = {rel: text for rel, text in files.items() if rel not in (layout.SCHEMA, layout.INDEX)}
    for rel, text in pages.items():
        if layout.slug(rel) is None:
            # Not a page (only the sandbox can put one there): nothing reads it, so it must
            # not stall every run; the owner sees it in the report.
            out.append(Finding(WARN, "file-name", rel, "memory/ 里不是页面的文件，不会被读取"))
            continue
        out += check_page(rel, text, known_source, allow_stub=True)
        if page.stub_target(text) is not None:
            out.append(Finding(BLOCK, "stub", rel, "MOVED TO 占位没有清掉"))
    if files.get(layout.SCHEMA) != schema_text:
        out.append(Finding(BLOCK, "schema", layout.SCHEMA, "_schema.md 被改动过"))
    out += _check_index(files.get(layout.INDEX, ""), pages)
    names = {layout.slug(rel) for rel in pages}
    aliases = {}
    for rel, text in pages.items():
        parsed = page.parse(text)
        for target in page.links(text):
            if target not in names: out.append(Finding(WARN, "broken-link", rel, "[[" + target + "]] 还没有页面"))
        if _repeated(text):
            out.append(Finding(WARN, "duplicate-fact", rel, str(len(_repeated(text))) + " 行事实在页面里重复出现，只保留一处"))
        if len(text) > layout.CURRENT_PICTURE_AFTER_CHARS and not page.has_current_picture(text):
            out.append(Finding(WARN, "current-picture", rel, "页面超过 8000 字符，开头缺少 Current picture"))
        for alias in parsed.aliases:
            aliases.setdefault(alias.casefold(), []).append(rel)
        if rel in new_pages and len(page.sources(text)) < 2:
            out.append(Finding(WARN, "single-source", rel, "新页面只有 1 个来源"))
    for alias, rels in aliases.items():
        if len(rels) > 1: out.append(Finding(WARN, "duplicate-alias", rels[0], "别名「" + alias + "」同时出现在 " + "、".join(rels)))
    total = sum(len(text.encode()) for text in pages.values())
    limit = layout.WIKI_MAX_BYTES if source_bytes is None else min(layout.WIKI_MAX_BYTES, int(source_bytes * layout.WIKI_MAX_RATIO))
    if source_bytes is not None and total > limit:
        out.append(Finding(WARN, "wiki-size", layout.DIR, "wiki 共 " + str(total) + " 字节，超过上限 " + str(limit)))
    return out


def _check_index(text, pages):
    out = []
    entries = "\n".join(line for line in text.splitlines() if line.lstrip().startswith("- [["))
    listed = set(page.LINK.findall("\n".join(re.findall(r"^\s*- \[\[[a-z0-9-]+\]\]", entries, re.M))))
    names = {layout.slug(rel): rel for rel in pages if layout.is_page(rel) and page.stub_target(pages[rel]) is None}
    for target in sorted(listed - set(names)):
        out.append(Finding(BLOCK, "index-entry", layout.INDEX, "[[" + target + "]] 指向不存在的页面"))
    for name in sorted(set(names) - listed):
        out.append(Finding(BLOCK, "index-entry", layout.INDEX, "页面 " + name + " 不在索引里"))
    # Where to start is injected into every turn: no instructions there. Elsewhere a phrase
    # like "执行脚本" is usually a procedure's description, so it only warns.
    start = page.section(text, layout.WHERE_TO_START)
    injected = set(start.splitlines())
    for line in text.splitlines():
        if INSTRUCTION.search(line):
            level = BLOCK if line in injected else WARN
            out.append(Finding(level, "index-instruction", layout.INDEX, "索引里不能有指令：" + line[:80]))
    if len(start.splitlines()) - 1 > layout.WHERE_TO_START_MAX_LINES:
        out.append(Finding(WARN, "where-to-start", layout.INDEX, "Where to start 超过 40 行"))
    return out
