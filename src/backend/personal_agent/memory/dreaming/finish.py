"""The end of a run, in code: clear stubs, lint, report, acknowledge, release; or roll back.

finish() raises LintBlocked on a blocking finding; abort() then restores the attempt's
snapshot (only while the run still holds its lease, so no one else's write is undone),
leaves every receipt undreamed and schedules a retry with exponential backoff.
"""
import hashlib
import logging
import re
from datetime import datetime

from sqlalchemy import select

from core.db.models import ChatMessage
from personal_agent.memory import layout, lint, page, seed, snapshots, sources, state as memory_state, store, usage, versions
from personal_agent.memory.dreaming import report, runs
from personal_agent.scheduling.schedules import user_zone, zone
from personal_agent.workspace import files

FAILURES_BEFORE_SKIP = 3
log = logging.getLogger(__name__)


class LintBlocked(RuntimeError):
    def __init__(self, findings):
        self.findings = findings
        super().__init__("; ".join(f.path + " " + f.rule + ": " + f.detail for f in findings[:10]))


def _stamp(now, agent):
    """The owner's local time: a 03:00 run is reported on the owner's day."""
    return datetime.fromtimestamp(now, zone(user_zone(agent.config))).isoformat(timespec="minutes")


def clear_stubs(db, agent, run_id):
    """Delete each MOVED TO stub whose target page exists; a stub left behind blocks lint."""
    wiki = store.wiki(agent.id)
    live = {layout.slug(rel) for rel, text in wiki.items() if layout.is_page(rel) and page.stub_target(text) is None}
    merged = []
    for rel, text in wiki.items():
        target = page.stub_target(text) if layout.is_page(rel) else None
        if target and target in live:
            store.remove(db, agent, rel, text, writer=layout.ORGANIZER, run_id=run_id)
            usage.move(db, agent.id, rel, layout.DIR + "/" + target + ".md")
            merged.append(layout.slug(rel) + " → " + target)
    return merged


def _known(db, agent, before):
    """Every new citation must exist; ones the wiki held before this run are not re-judged,
    so a conversation the owner deleted later cannot block every future run."""
    exists = sources.checker(db, agent)
    old = {sid for text in before.values() for sid in page.sources(text)}
    return lambda sid: sid in old or exists(sid)


def _conflicts(before, after):
    out = []
    for rel, text in after.items():
        seen = set((before.get(rel) or "").splitlines())
        out += [(rel, line.strip()[:300]) for line in text.splitlines() if "CONFLICT:" in line and line not in seen]
    return out


def _pages(db, agent, run_id, merged):
    rows = versions.listing(db, agent.id, run_id=run_id, limit=1000)
    created, changed = set(), set()
    for row in rows:
        if not layout.is_page(row.path) or row.after_hash is None: continue
        (created if row.before_hash is None else changed).add(row.path)
    wiki = store.wiki(agent.id)
    created = {rel for rel in created if rel in wiki}
    return {"created": sorted(created), "changed": sorted((changed - created) & set(wiki)), "merged": merged}


def _receipt(db, message_id):
    row = db.get(ChatMessage, message_id)
    return (row, dict((row.extra_data or {}).get(runs.RECEIPT) or {})) if row is not None else (None, None)


def _count_failure(row, receipt, genesis):
    """One more failed analysis; a receipt that keeps failing is skipped so it cannot stall Dreaming."""
    receipt["dream_failures"] = receipt.get("dream_failures", 0) + 1
    if receipt["dream_failures"] >= FAILURES_BEFORE_SKIP and not genesis:
        receipt.update(dreamed=True, dream_skipped="analyzer failed " + str(FAILURES_BEFORE_SKIP) + " times")
    row.extra_data = {**row.extra_data, runs.RECEIPT: receipt}


def _acknowledge(db, batch, results):
    """Dreamed: every part analyzed."""
    failed = {item["receipt"] for item in results if not item["ok"]}
    for message_id in batch.receipts:
        row, receipt = _receipt(db, message_id)
        if row is None: continue
        if message_id in failed:
            _count_failure(row, receipt, batch.genesis)
            continue
        receipt.update(dreamed=True, dream_failures=0)
        row.extra_data = {**row.extra_data, runs.RECEIPT: receipt}


def journal_orphans(db, agent, since):
    """Journal entries since genesis began whose message no longer exists (not replayable)."""
    known = {hashlib.sha256(ident.encode()).hexdigest() for ident in db.scalars(select(ChatMessage.message_id).where(
        ChatMessage.extra_data[runs.RECEIPT]["agent_id"].as_string() == agent.id))}
    day = str(since or "")[:10]
    root = files.local_workspace(agent.id) / layout.JOURNAL_DIR
    count = 0
    if not root.is_dir(): return 0
    for path in sorted(root.glob("*.md")):
        if path.stem < day: continue
        try: text = path.read_text(encoding="utf-8", errors="replace")
        except OSError: continue
        count += sum(1 for marker in re.findall(r"<!-- pa-journal:([0-9a-f]{64}) -->", text) if marker not in known)
    return count


def finish(sessions, ident, run_id, owner, batch, results, organized, before, now):
    """Everything after the organizer; raises LintBlocked or LeaseLost."""
    with sessions() as db:
        agent, _row, _data = runs.owned(db, ident, run_id, owner)
        merged = clear_stubs(db, agent, run_id)
        db.commit()
    with sessions() as db:
        agent, row, data = runs.owned(db, ident, run_id, owner)
        after = store.wiki(ident)
        pages = _pages(db, agent, run_id, merged)
        findings = lint.check_wiki(after, seed.schema_text(), _known(db, agent, before),
                                   new_pages=pages["created"], source_bytes=batch.source_bytes)
        blocking = lint.blocking(findings)
        if blocking: raise LintBlocked(blocking)
        online = {key: sum(item.get("online_changes", {}).get(key, 0) for item in results if item["ok"]) for key in ("kept", "revised", "reverted")}
        genesis_done = batch.genesis and batch.exhausted
        orphans = journal_orphans(db, agent, memory_state.genesis(agent).get("since")) if genesis_done else 0
        life = organized.get("lifecycle") or {}
        content = {"results": results, "skipped": batch.skipped, "pages": pages, "online": online, "lifecycle": life,
                   "conflicts": _conflicts(before, after), "warnings": findings, "organizer": organized,
                   "suggestions": report.remember_suggestions(ident, batch.sources),
                   "genesis": batch.genesis, "genesis_done": genesis_done, "orphans": orphans}
        body = report.body(run_id, _stamp(now, agent), content)
        _acknowledge(db, batch, results)
        if batch.genesis:
            if genesis_done: memory_state.set_genesis(agent, "done", cursor=batch.cursor, finished_at=_stamp(now, agent))
            else: memory_state.set_genesis(agent, "running", cursor=batch.cursor)
        db.flush()  # production sessions do not autoflush; the pending query must see the acknowledgements
        progressed = any(item["ok"] for item in results) or bool(batch.skipped)
        more = (batch.genesis and not genesis_done) or runs.pending_count(db, agent, cap=1) > 0
        outcomes = ["来源 " + str(sum(1 for item in results if item["ok"])) + "/" + str(len(results)),
                    "新建页面 " + str(len(pages["created"])), "修改页面 " + str(len(pages["changed"])),
                    "合并 " + str(len(merged)), "归档 " + str(len(life.get("archived", ()))) + " / 删除 " + str(len(life.get("deleted", ()))), "白天改动 保留 " + str(online["kept"]) + " / 修正 " + str(online["revised"]) + " / 撤回 " + str(online["reverted"]),
                    "lint 告警 " + str(len(findings))]
        _done(agent, row, data, now, outcomes, continuation=more and progressed, source_bytes=batch.source_bytes, owner=owner)
        db.commit()
    # After the commit: a report must never describe a run that was rolled back.
    _append_report(ident, run_id, body)


def _append_report(ident, key, body):
    try:
        with files.workspace_lock(ident):
            report.append(ident, key, body)
    except (OSError, RuntimeError, ValueError) as exc:
        log.warning("Dreaming report not written (%s)", type(exc).__name__)


def _done(agent, row, data, now, outcomes, *, continuation=False, source_bytes=None, owner):
    data.update(status="done", completed_at=now, error=None, next_retry=0, outcomes=outcomes, continuation=continuation)
    runs.save(row, data)
    dreaming = {**(agent.config.get("dreaming_state") or {}), "last_completed_at": now}
    if source_bytes is not None: dreaming["source_bytes"] = source_bytes
    agent.config = {**agent.config, "dreaming_state": dreaming}
    memory_state.release(agent, owner)


def empty(sessions, ident, run_id, owner, batch, now):
    """Nothing to dream (only skipped receipts, or a manual run with an empty queue)."""
    with sessions() as db:
        agent, row, data = runs.owned(db, ident, run_id, owner)
        if batch.genesis and batch.exhausted:
            memory_state.set_genesis(agent, "done", cursor=batch.cursor, finished_at=_stamp(now, agent))
        elif batch.genesis and batch.cursor:
            # A window of only skipped receipts still moves the replay forward.
            memory_state.set_genesis(agent, "running", cursor=batch.cursor)
        outcomes = ["没有新的来源"] + (["跳过 " + str(len(batch.skipped)) + " 条已删除对话的记录"] if batch.skipped else [])
        more = (batch.genesis and not batch.exhausted) or runs.pending_count(db, agent, cap=1) > 0
        _done(agent, row, data, now, outcomes, continuation=bool(batch.skipped) and more, owner=owner)
        db.commit()


def abort(sessions, ident, run_id, owner, snapshot_id, exc, now, failed_receipts=()):
    """Roll back to this attempt's snapshot while no one else took the lease, then back off."""
    restored = False
    with sessions() as db:
        agent = runs.locked(db, ident)
        row = runs.current(db, agent) if agent else None
        if row is None or row.message_id != run_id: return
        data = runs.run_state(row)
        if data.get("owner") != owner: return
        # Ours even when it expired (a slow step): it still names us, so no one else wrote
        # under it, and only online writes after the expiry can be lost by the rollback.
        ours = ((agent.config or {}).get(memory_state.LEASE) or {}).get("owner") == owner
        for message_id in failed_receipts:
            receipt_row, receipt = _receipt(db, message_id)
            if receipt_row is not None: _count_failure(receipt_row, receipt, False)
        if snapshot_id and ours:
            try:
                with files.workspace_lock(ident):
                    snapshots.restore(db, agent, snapshot_id, run_id=run_id)
                restored = True
            except (OSError, LookupError, ValueError) as error:
                log.warning("Dreaming rollback failed (%s)", type(error).__name__)
        error = type(exc).__name__ + (": " + str(exc)[:300] if isinstance(exc, LintBlocked) else "")
        attempts = data.get("attempts", 1)
        data.update(status="failed" if attempts >= runs.MAX_ATTEMPTS else "retry", error=error,
                    next_retry=now + min(3600, 60 * 2 ** (attempts - 1)), rolled_back=restored)
        runs.save(row, data)
        memory_state.release(agent, owner)
        db.commit()
        stamp = _stamp(now, agent)
    note = ("## Dreaming attempt " + str(attempts) + " · " + stamp + "\n\n结果：" + ("失败，" if data["status"] == "failed" else "将重试，")
            + error + ("；记忆已恢复到这次整理之前" if restored else "") + "。待处理的来源没有确认，会在下次重新处理。\n")
    # The durable status stays visible in settings even when the report cannot be written.
    _append_report(ident, run_id + "-attempt-" + str(attempts), note.replace("<!--", "&lt;!--"))
