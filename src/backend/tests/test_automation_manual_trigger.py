"""Regression: 「立即执行」必须真的执行。

路由原本直接 `asyncio.create_task(scheduler.execute_task(...))`，在生产的多 worker +
同步路由下 100% 失败（503 或 `no running event loop`），而定时触发照常——所以这条路径
必须单独有测试。改成经 `manual_trigger_at` 交接后，这里盯的就是交接本身。
"""

from core.services.automation_service import AutomationService


def _task(db, **kw):
    svc = AutomationService(db)
    return svc, svc.create_task(
        user_id="u1",
        task_type="prompt",
        prompt="跑一次",
        cron_expression="30 10 29 6 *",
        **kw,
    )


def test_request_marks_task_and_drain_takes_it_once(db_session):
    svc, task = _task(db_session)
    assert task.manual_trigger_at is None

    svc.request_manual_trigger(task)
    db_session.refresh(task)
    assert task.manual_trigger_at is not None

    assert svc.take_manual_triggers() == [(task.task_id, "u1")]

    # 取走即清空：第二次排空不能把同一次点击再发一遍。
    db_session.refresh(task)
    assert task.manual_trigger_at is None
    assert svc.take_manual_triggers() == []


def test_repeated_clicks_queue_once(db_session):
    svc, task = _task(db_session)
    svc.request_manual_trigger(task)
    svc.request_manual_trigger(task)
    svc.request_manual_trigger(task)

    assert svc.take_manual_triggers() == [(task.task_id, "u1")]


def test_paused_task_can_still_be_queued(db_session):
    """暂停中的任务允许手动跑一次——状态校验在路由，这里确认交接本身不挑状态。"""
    svc, task = _task(db_session)
    svc.pause_task(task.task_id, "u1")

    svc.request_manual_trigger(task)
    assert svc.take_manual_triggers() == [(task.task_id, "u1")]


def test_manual_trigger_does_not_move_the_schedule(db_session):
    """手动跑一次不该打乱 cron 排期——next_run_at 由调度器推进，这条路径不碰它。"""
    svc, task = _task(db_session)
    before = task.next_run_at

    svc.request_manual_trigger(task)
    svc.take_manual_triggers()
    db_session.refresh(task)

    assert task.next_run_at == before


def test_bump_run_count_increments_in_place(db_session):
    """排空时补的计数是原地自增，不是读-改-写，免得和 advance_next_run 互相覆盖。"""
    svc, task = _task(db_session)
    assert (task.run_count or 0) == 0

    svc.bump_run_count(task.task_id)
    svc.bump_run_count(task.task_id)
    db_session.refresh(task)

    assert task.run_count == 2
