"""The two model steps of a run: one analyzer per source, serially, then one organizer."""
import asyncio
import functools
import logging
from pathlib import Path

from personal_agent.memory import layout, lifecycle as memory_lifecycle, lint, page, store
from personal_agent.memory.dreaming import loop, runs
from personal_agent.memory.dreaming.tools import Toolbox

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
log = logging.getLogger(__name__)


@functools.cache
def system_prompt(name):
    return (PROMPTS / (name + ".system.md")).read_text(encoding="utf-8").strip()


def _renewer(sessions, ident, run_id, owner):
    return lambda: asyncio.to_thread(runs.renew, sessions, ident, run_id, owner)


def _count(value):
    return value if type(value) is int and 0 <= value <= 10_000 else 0


async def analyze(sessions, ident, run_id, owner, model, source, today):
    """One source; one retry. {"ok", "sid", "summary", "pages_written", "online_changes", "error"}."""
    renew = _renewer(sessions, ident, run_id, owner)
    part = "（第 " + str(source["part"]) + "/" + str(source["parts"]) + " 部分）" if source["parts"] > 1 else ""
    task = ("今天是 " + today + "。你负责的来源文件：" + source["path"] + part + "，来源 id：" + source["sid"]
            + "。白天由它写下的记忆改动：" + str(source["changes"]) + " 处" + ("，列在来源文件末尾" if source["changes"] else "")
            + "。按系统提示词的方法核实、补全、整理 memory/，完成后只回复一行 JSON。")
    error = None
    for _attempt in range(2):
        box = Toolbox(sessions, ident, run_id, owner, layout.ANALYZER, source_id=source["sid"],
                      sources={source["path"]: source["text"]})
        try:
            result = loop.answer(await loop.run(model, system_prompt("analyzer"), task, box, renew=renew))
        except runs.LeaseLost:
            raise
        except Exception as exc:
            error = type(exc).__name__ + (": " + str(exc)[:200] if str(exc) else "")
            log.warning("Dreaming analyzer failed for %s (%s)", source["sid"], type(exc).__name__)
            # The retry, and the next run, start from the wiki this source had not touched.
            await asyncio.to_thread(undo, sessions, ident, run_id, owner, source["sid"])
            continue
        changes = result.get("online_changes") if isinstance(result.get("online_changes"), dict) else {}
        summary = result.get("summary") if isinstance(result.get("summary"), str) else ""
        return {"ok": True, "sid": source["sid"], "receipt": source["receipt"], "summary": summary[:1000],
                "pages_written": sorted(set(box.written)),
                "online_changes": {key: _count(changes.get(key)) for key in ("kept", "revised", "reverted")}}
    return {"ok": False, "sid": source["sid"], "receipt": source["receipt"], "error": error}


def undo(sessions, ident, run_id, owner, source_id):
    """Revert this run's writes for one source, newest first."""
    from personal_agent.memory import versions
    with sessions() as db:
        agent, _row, _data = runs.owned(db, ident, run_id, owner)
        for row in versions.listing(db, ident, run_id=run_id, source_id=source_id, limit=1000):
            if store.revertable(row, store.read(ident, row.path)): store.revert(db, agent, row.id)
        db.commit()


def lifecycle(sessions, ident, run_id, owner, today):
    """Archive, restore and delete pages by use (memory.lifecycle), before the organizer
    rebuilds the index around the result."""
    with sessions() as db:
        agent, _row, _data = runs.owned(db, ident, run_id, owner)
        out = memory_lifecycle.apply(db, agent, today, run_id=run_id)
        db.commit()
    return out


def brief(ident, source_bytes, today, analyzers, life=None):
    """The organizer's task message: what the wiki looks like tonight."""
    wiki = store.wiki(ident)
    pages = {rel: text for rel, text in wiki.items() if layout.is_page(rel)}
    total = sum(len(text.encode()) for text in pages.values())
    limit = min(layout.WIKI_MAX_BYTES, int(source_bytes * layout.WIKI_MAX_RATIO)) if source_bytes else layout.WIKI_MAX_BYTES
    large, archived = [], []
    for rel, text in sorted(pages.items()):
        if page.stub_target(text) is not None: continue
        if len(text) > layout.CURRENT_PICTURE_AFTER_CHARS: large.append(rel + "（" + str(len(text)) + " 字符）")
        if page.parse(text).meta.get("status") == "archived": archived.append(layout.slug(rel))
    life = life or {}
    pending = page.links(page.section(wiki.get(layout.INDEX, ""), layout.PENDING_HEADING))
    findings = lint.check_wiki(wiki, wiki.get(layout.SCHEMA, ""), None, source_bytes=source_bytes or None)
    blocking = lint.blocking(findings)
    warnings = [f for f in findings if f.level == lint.WARN]
    over = total > limit
    lines = ["今天是 " + today + "。今晚有 " + str(analyzers) + " 个 analyzer 跑完。",
             "页面 " + str(len(pages)) + " 个，共 " + str(total) + " 字节；源材料共 " + str(source_bytes) + " 字节，总量上限 " + str(limit) + " 字节。",
             ("总量已超过上限：只合并和归档，不新建页面。" if over else "总量没有超过上限。"),
             "超过 8000 字符、要压缩的页面：" + ("、".join(large) or "无"),
             "平台今晚按使用情况归档：" + ("、".join(life.get("archived", ())) or "无") + "；恢复：" + ("、".join(life.get("restored", ())) or "无")
             + "；删除：" + ("、".join(life.get("deleted", ())) or "无"),
             "现在已归档的页面（放进 ## 已归档，不进 Where to start）：" + ("、".join(archived) or "无"),
             "## 待整理 里的页面：" + ("、".join(pending) or "无"),
             "必须修好、否则整晚回滚的问题：" + ("；".join(f.path + " " + f.detail for f in blocking[:40]) or "无"),
             "lint 告警：" + ("；".join(f.path + " " + f.detail for f in warnings[:40]) or "无"),
             "按系统提示词整理整个 memory/，完成后只回复一行 JSON。"]
    return "\n".join(lines)


async def organize(sessions, ident, run_id, owner, model, task):
    renew = _renewer(sessions, ident, run_id, owner)
    box = Toolbox(sessions, ident, run_id, owner, layout.ORGANIZER)
    result = loop.answer(await loop.run(model, system_prompt("organizer"), task, box, renew=renew))
    changes = result.get("changes") if isinstance(result.get("changes"), list) else []
    return {"index_entries": _count(result.get("index_entries")),
            "changes": [str(item)[:300] for item in changes[:60]], "pages_written": sorted(set(box.written))}
