"""Dreaming when things go wrong: a crashed attempt, a source that keeps failing, a failed run."""
from personal_agent.memory import layout, state as memory_state, store
from personal_agent.memory.dreaming import collect, runs
from personal_agent.models import PersonalAgent
from tests.personal_agent.dreaming_support import (  # noqa: F401
    NOW, Script, agent, personal_agent_on, call, conversation, fact, page_text, receipt, run_view, say, sessions, tick)

BUDGET = "memory/project-q4-budget.md"


def _budget(sid):
    return page_text("project-q4-budget", ["## 金额", fact("55 万", sid)])


def test_an_attempt_that_died_mid_run_is_rolled_back_before_the_next(agent, sessions):
    ident = agent
    sid = conversation(sessions, "Q4 预算 55 万", "好")
    run_id, owner, _attempt = runs.claim(sessions, ident, NOW)
    collect.prepare(sessions, ident, run_id, owner)
    collect.snapshot(sessions, ident, run_id, owner)
    with sessions() as db:
        agent_row, _row, _data = runs.owned(db, ident, run_id, owner)
        store.write(db, agent_row, BUDGET, _budget(sid), None, writer=layout.ANALYZER, source_id=sid, run_id=run_id)
        # The process dies here: no abort(), the run row stays "running" and the lease runs out.
        agent_row.config = {**agent_row.config, memory_state.LEASE: {"owner": owner, "until": 0}}
        db.commit()

    tick(sessions, Script(analyzer=[[[say('{"summary": "无事可记"}')]]], organizer=[[say('{"index_entries": 0}')]]), now=NOW + 60)
    assert store.read(ident, BUDGET) is None   # the dead attempt's page did not become the new baseline
    status = run_view(sessions)
    assert status["status"] == "done" and status["attempts"] == 2 and receipt(sessions, sid)["dreamed"] is True


def test_a_failed_analyzer_leaves_no_writes_and_counts_against_its_source(agent, sessions):
    ident = agent
    sid = conversation(sessions, "Q4 预算 55 万", "好")
    write_then_fail = [[call("write", path=BUDGET, content=_budget(sid))], RuntimeError("context too long")]
    tick(sessions, Script(analyzer=[list(write_then_fail), list(write_then_fail)]))
    assert store.read(ident, BUDGET) is None
    assert receipt(sessions, sid)["dream_failures"] == 1 and receipt(sessions, sid).get("dreamed") is not True
    assert run_view(sessions)["status"] == "retry"


def test_a_failed_run_does_not_stop_the_next_nightly_slot(agent, sessions):
    ident = agent
    conversation(sessions, "Q4 预算 55 万", "好")
    run_id, _owner, _attempt = runs.claim(sessions, ident, NOW)
    with sessions() as db:
        row = db.get(PersonalAgent, ident)
        current = runs.current(db, row)
        runs.save(current, {**runs.run_state(current), "status": "failed"})
        memory_state.release(row, _owner)
        db.commit()
    assert runs.claim(sessions, ident, NOW + 60) is None          # same day: waits for "run now"
    claimed = runs.claim(sessions, ident, NOW + 86400)            # the next night starts a new run
    assert claimed is not None and claimed[0] != run_id
