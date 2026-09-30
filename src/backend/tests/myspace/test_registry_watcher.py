"""文件系统驱动的「我的空间」登记器。

回归的是这条真实故障：登记原来挂在会写文件的那几个工具上（write / edit / 文件增删改 /
bash 前后快照），而后台进程、子智能体、技能 CLI、MCP 服务端都会往 ``/myspace`` 写，漏掉
的那些文件在沙箱里看得见、界面上看不见也删不掉。生产上某账号顶层 143 个文件里有 133 个
处在这个状态。现在判据换成文件系统本身，这里验证的就是"谁写的都一样"。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from core.llm import workspace
from core.myspace import mirror
from core.space_sync import personal as watcher


@pytest.fixture
def registry(monkeypatch):
    """一个不启观察者线程的登记器：直接驱动它的处理逻辑，不依赖真实文件事件的时序。"""
    reg = watcher.MySpaceRegistry()
    reg._root = Path("/tmp/jx-myspace-test")
    reg._budget = watcher.Budget(watcher._INFLIGHT_BUDGET_BYTES)
    monkeypatch.setattr(watcher, "_claim", _always_claim)
    monkeypatch.setattr(watcher, "_HANDOFF_GRACE_S", 0)
    _stub_preset(monkeypatch)  # 默认：权限档没有替用户答，该问还是要问
    return reg


async def _always_claim(user_id, rel, stamp):
    return True


async def _never_claim(user_id, rel, stamp):
    return False


def _entry(rel: str, *, mtime: float = 100.0, size: int = 10):
    return mirror.MirrorEntry(rel=rel, path=Path("/tmp") / rel, size=size, mtime=mtime)


_REG = mirror.RegisteredFile(artifact_id="a1", storage_key="k", registered_ts=1.0)


def _stub_present(monkeypatch, verdict, entry=None):
    """磁盘上有这些文件，判定为 ``verdict``。"""
    monkeypatch.setattr(
        watcher, "_stat_all", lambda uid, rels: {rel: entry or _entry(rel) for rel in rels}
    )
    monkeypatch.setattr(
        mirror, "classify_claimed", lambda *, user_id, entries: {rel: verdict for rel in entries}
    )


def _stub_gone(monkeypatch, target):
    """磁盘上这些路径没了，账本里对应 ``target``（``None`` = 从没登记过）。"""
    monkeypatch.setattr(watcher, "_stat_all", lambda uid, rels: {rel: None for rel in rels})
    monkeypatch.setattr(
        mirror, "classify_deletes", lambda *, user_id, rels: {rel: target for rel in rels}
    )


def _capture_registrations(monkeypatch) -> list:
    seen: list = []

    def _register(*, user_id, entry):
        seen.append((user_id, entry.rel))
        return {"file_id": "fid_1", "name": entry.rel}

    monkeypatch.setattr(mirror, "register_entry", _register)
    return seen


def _stub_preset(monkeypatch, answered: bool = False):
    monkeypatch.setattr(
        "core.llm.tool_permissions.preset_answers_for_user",
        lambda user_id, *, op="", dangerous=False: answered,
    )


def _stub_gate(monkeypatch, *, allow: bool):
    async def _gate(**kwargs):
        return None if allow else {"status": "blocked"}

    monkeypatch.setattr("core.llm.tools._myspace_confirm.gate", _gate)


# ── 路径归属 ──────────────────────────────────────────────────────────────


def test_split_extracts_user_and_relative_path():
    root = Path("/srv/storage/myspace_cache")
    assert watcher._split("/srv/storage/myspace_cache/u1/a/b.txt", root) == ("u1", "a/b.txt")


def test_split_ignores_the_user_directory_itself():
    root = Path("/srv/storage/myspace_cache")
    assert watcher._split("/srv/storage/myspace_cache/u1", root) is None


def test_split_ignores_runtime_junk_directories():
    root = Path("/srv/storage/myspace_cache")
    assert watcher._split("/srv/storage/myspace_cache/u1/__pycache__/x.pyc", root) is None


def test_split_ignores_paths_outside_the_root():
    root = Path("/srv/storage/myspace_cache")
    assert watcher._split("/etc/passwd", root) is None


# ── 登记：谁写的都一样 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_new_file_is_registered_without_asking(registry, monkeypatch):
    """新文件直接登记 —— 内容已经落在用户自己的目录里，此时打断用户没有意义。"""
    _stub_present(monkeypatch, mirror.VERDICT_NEW)
    seen = _capture_registrations(monkeypatch)
    monkeypatch.setattr("core.space_sync.personal_policy.find_chat", lambda uid: "chat-1")

    async def _must_not_ask(**kwargs):
        raise AssertionError("新文件不该弹确认")

    monkeypatch.setattr("core.llm.tools._myspace_confirm.gate", _must_not_ask)

    await registry._process("u1", ["报告.docx"])
    assert seen == [("u1", "报告.docx")]


@pytest.mark.asyncio
async def test_the_registry_never_pins_a_card(registry, monkeypatch):
    """登记器只登记，不往任何会话的产物区里挂卡片。

    回归的是这条真实故障：会话 A 在生成 Word，会话 B 的沙箱同时往「我的空间」里复制了一个
    csv。「我的空间」是用户级的一份目录、每个会话的沙箱都挂着它，文件事件里只有路径、没有
    会话身份，登记器却抓了"这个用户当下随便哪个在跑的会话"顶上，于是 csv 记到了 A 名下，
    还在 A 的产物区里多出一张卡片。

    断言落在 ``core.llm.workspace`` 的状态上 —— 所有挂卡路径都汇到它，换条路挂也照样兜得住。
    """
    _stub_present(monkeypatch, mirror.VERDICT_NEW)
    seen = _capture_registrations(monkeypatch)
    monkeypatch.setattr("core.space_sync.personal_policy.find_chat", lambda uid: "chat-A")
    workspace.init_state()

    await registry._process("u1", ["test_data.csv"])
    assert seen, "文件本身应该照常登记进「我的空间」"
    assert workspace.get_pinned_file_ids() == []


@pytest.mark.asyncio
async def test_a_file_written_by_a_background_process_is_still_registered(registry, monkeypatch):
    """没有活跃会话（nohup 起的进程在命令返回之后才写完）照样登记。"""
    _stub_present(monkeypatch, mirror.VERDICT_NEW)
    seen = _capture_registrations(monkeypatch)
    monkeypatch.setattr("core.space_sync.personal_policy.find_chat", lambda uid: None)

    await registry._process("u1", ["run/结果.json"])
    assert seen == [("u1", "run/结果.json")]


@pytest.mark.asyncio
async def test_an_already_registered_file_is_not_registered_again(registry, monkeypatch):
    """账本已经反映了这份内容 —— 后端自己刚写下去的（正向同步）就是这一类。"""
    _stub_present(monkeypatch, mirror.VERDICT_CURRENT)
    seen = _capture_registrations(monkeypatch)
    monkeypatch.setattr("core.space_sync.personal_policy.find_chat", lambda uid: "chat-1")

    await registry._process("u1", ["报告.docx"])
    assert seen == []


@pytest.mark.asyncio
async def test_a_file_the_user_deleted_is_never_resurrected(registry, monkeypatch):
    _stub_present(monkeypatch, mirror.VERDICT_STALE)
    seen = _capture_registrations(monkeypatch)
    monkeypatch.setattr("core.space_sync.personal_policy.find_chat", lambda uid: None)

    await registry._process("u1", ["旧稿.docx"])
    assert seen == []


@pytest.mark.asyncio
async def test_one_change_is_handled_once_across_workers(registry, monkeypatch):
    """每个 worker 都监听同一份目录，没抢到认领的那个不该重复登记。"""
    _stub_present(monkeypatch, mirror.VERDICT_NEW)
    seen = _capture_registrations(monkeypatch)
    monkeypatch.setattr("core.space_sync.personal_policy.find_chat", lambda uid: None)
    monkeypatch.setattr(watcher, "_claim", _never_claim)

    await registry._process("u1", ["报告.docx"])
    assert seen == []


# ── 改写用户已有文件：确认门变成"退回去" ──────────────────────────────────


@pytest.mark.asyncio
async def test_overwriting_a_user_file_goes_through_the_confirmation(registry, monkeypatch):
    _stub_present(monkeypatch, mirror.VERDICT_MODIFIED)
    seen = _capture_registrations(monkeypatch)
    monkeypatch.setattr("core.space_sync.personal_policy.find_chat", lambda uid: "chat-1")
    asked: list = []

    async def _gate(**kwargs):
        asked.append(kwargs["logical_path"])
        return None

    monkeypatch.setattr("core.llm.tools._myspace_confirm.gate", _gate)

    await registry._process("u1", ["年度总结.docx"])
    assert asked == ["/myspace/年度总结.docx"]
    assert seen == [("u1", "年度总结.docx")]


@pytest.mark.asyncio
async def test_a_refused_overwrite_restores_the_registered_version(registry, monkeypatch):
    """文件在磁盘上早改完了，拒绝只能是把账本里那一版还原回去。"""
    _stub_present(monkeypatch, mirror.VERDICT_MODIFIED)
    seen = _capture_registrations(monkeypatch)
    monkeypatch.setattr("core.space_sync.personal_policy.find_chat", lambda uid: "chat-1")
    _stub_gate(monkeypatch, allow=False)
    restored: list = []
    monkeypatch.setattr(
        mirror,
        "restore_from_registry",
        lambda *, user_id, rel, reg=None: restored.append(rel) or True,
    )

    await registry._process("u1", ["年度总结.docx"])
    assert restored == ["年度总结.docx"]
    assert seen == []


@pytest.mark.asyncio
async def test_a_subagent_overwrite_takes_effect_without_asking(registry, monkeypatch):
    """问不到人时直接生效 —— 拒绝拦不住已经发生的改动，只会让两边不一致。"""
    _stub_present(monkeypatch, mirror.VERDICT_MODIFIED)
    seen = _capture_registrations(monkeypatch)
    monkeypatch.setattr("core.space_sync.personal_policy.find_chat", lambda uid: None)

    async def _must_not_ask(**kwargs):
        raise AssertionError("没有活跃会话时不该弹确认")

    monkeypatch.setattr("core.llm.tools._myspace_confirm.gate", _must_not_ask)

    await registry._process("u1", ["年度总结.docx"])
    assert seen == [("u1", "年度总结.docx")]


# ── 删除：沙箱里 rm 掉的文件要从「我的空间」消失 ──────────────────────────


@pytest.mark.asyncio
async def test_rm_in_the_sandbox_removes_the_file_from_myspace(registry, monkeypatch):
    _stub_gone(monkeypatch, mirror.DeleteTarget(registered=_REG))
    monkeypatch.setattr("core.space_sync.personal_policy.find_chat", lambda uid: "chat-1")
    _stub_gate(monkeypatch, allow=True)
    deleted: list = []
    monkeypatch.setattr(
        mirror, "delete_registered", lambda *, user_id, rel, target=None: deleted.append(rel) or True
    )

    await registry._process("u1", ["草稿.docx"])
    assert deleted == ["草稿.docx"]


@pytest.mark.asyncio
async def test_a_refused_deletion_restores_the_file(registry, monkeypatch):
    _stub_gone(monkeypatch, mirror.DeleteTarget(registered=_REG))
    monkeypatch.setattr("core.space_sync.personal_policy.find_chat", lambda uid: "chat-1")
    _stub_gate(monkeypatch, allow=False)
    restored: list = []
    monkeypatch.setattr(
        mirror,
        "restore_from_registry",
        lambda *, user_id, rel, reg=None: restored.append(rel) or True,
    )
    monkeypatch.setattr(
        mirror,
        "delete_registered",
        lambda *, user_id, rel, target=None: (_ for _ in ()).throw(AssertionError("拒绝后不该删")),
    )

    await registry._process("u1", ["草稿.docx"])
    assert restored == ["草稿.docx"]


@pytest.mark.asyncio
async def test_an_unregistered_file_vanishing_is_a_no_op(registry, monkeypatch):
    """从没登记过的文件消失了，本来就不在用户空间里，不必也无从同步。"""
    _stub_gone(monkeypatch, None)
    monkeypatch.setattr("core.space_sync.personal_policy.find_chat", lambda uid: None)
    monkeypatch.setattr(
        mirror,
        "delete_registered",
        lambda *, user_id, rel, target=None: (_ for _ in ()).throw(AssertionError("不该删")),
    )

    await registry._process("u1", ["临时.tmp"])


@pytest.mark.asyncio
async def test_removing_a_folder_soft_deletes_what_is_inside(registry, monkeypatch):
    """``rm -rf /myspace/<目录>`` 只让路径消失，得认出删的是个还在册的文件夹。"""
    _stub_gone(monkeypatch, mirror.DeleteTarget(folder=True))
    monkeypatch.setattr("core.space_sync.personal_policy.find_chat", lambda uid: None)
    deleted: list = []
    monkeypatch.setattr(
        mirror, "delete_registered", lambda *, user_id, rel, target=None: deleted.append(rel) or True
    )

    await registry._process("u1", ["大优强_run"])
    assert deleted == ["大优强_run"]


# ── 成批处理与限流 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_burst_is_judged_in_one_pass(registry, monkeypatch):
    """一条命令往同一个目录里落几百个文件是常事，判定不该逐个开会话重来一遍。"""
    calls: list = []

    def _classify(*, user_id, entries):
        calls.append(list(entries))
        return {rel: mirror.VERDICT_NEW for rel in entries}

    monkeypatch.setattr(watcher, "_stat_all", lambda uid, rels: {rel: _entry(rel) for rel in rels})
    monkeypatch.setattr(mirror, "classify_claimed", _classify)
    seen = _capture_registrations(monkeypatch)
    monkeypatch.setattr("core.space_sync.personal_policy.find_chat", lambda uid: None)

    rels = [f"分片/{i}.tsv" for i in range(200)]
    await registry._process("u1", rels)

    assert len(calls) == 1 and len(calls[0]) == 200
    assert len(seen) == 200


@pytest.mark.asyncio
async def test_big_files_do_not_stack_up_in_memory():
    """限流按字节算：上限大小的文件只能一个一个来，小文件照旧并行。"""
    budget = watcher.Budget(watcher._INFLIGHT_BUDGET_BYTES)
    huge = watcher._INFLIGHT_BUDGET_BYTES  # 单个就吃满额度
    assert budget.cost(huge) == watcher._INFLIGHT_BUDGET_BYTES
    small = budget.cost(1024)
    assert watcher._INFLIGHT_BUDGET_BYTES // small == 8  # 小文件的并发度和从前一致

    entered = []

    async def _hold(size, release):
        async with budget.reserve(size):
            entered.append(size)
            await release.wait()

    first_done = asyncio.Event()
    t1 = asyncio.create_task(_hold(huge, first_done))
    await asyncio.sleep(0.02)
    t2 = asyncio.create_task(_hold(huge, first_done))
    await asyncio.sleep(0.02)
    assert entered == [huge]  # 第二个大文件还在等
    first_done.set()
    await asyncio.gather(t1, t2)
    assert entered == [huge, huge]


# ── 催办：写完立刻查得到 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_flush_processes_this_user_without_waiting_for_the_settle_window(
    registry, monkeypatch
):
    _stub_present(monkeypatch, mirror.VERDICT_NEW)
    seen = _capture_registrations(monkeypatch)
    monkeypatch.setattr("core.space_sync.personal_policy.find_chat", lambda uid: None)
    registry._mark(("u1", "刚写的.docx"))
    registry._mark(("u2", "别人的.docx"))

    await registry.flush("u1")
    assert seen == [("u1", "刚写的.docx")]
    assert ("u2", "别人的.docx") in registry._pending


# ── 端到端：真实文件事件 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_real_write_reaches_the_registry(tmp_path, monkeypatch):
    """起真观察者，往目录里写个文件，看它有没有被登记。

    验的是这套机制的地基：镜像目录是 bind mount，写它的是另一个容器里的进程，事件能不能
    到达本进程决定了整件事成不成立。判定和登记都换成假的，只看"事件走没走通"。
    """
    root = tmp_path / "myspace_cache"
    (root / "u1").mkdir(parents=True)
    monkeypatch.setattr(watcher, "myspace_cache_root", lambda: root)
    monkeypatch.setattr(watcher, "_SETTLE_S", 0.2)
    monkeypatch.setattr(watcher, "_HANDOFF_GRACE_S", 0)
    monkeypatch.setattr(watcher, "_claim", _always_claim)
    monkeypatch.setattr("core.space_sync.personal_policy.find_chat", lambda uid: None)
    _stub_present(monkeypatch, mirror.VERDICT_NEW)
    seen = _capture_registrations(monkeypatch)

    monkeypatch.setattr("core.space_sync.personal_database.projection_owners", lambda: set())
    reg = watcher.MySpaceRegistry()
    await reg.start()
    try:
        (root / "u1" / "命令写的.txt").write_text("hi", encoding="utf-8")
        for _ in range(100):
            await asyncio.sleep(0.05)
            if seen:
                break
    finally:
        await reg.stop()

    assert [rel for _u, rel in seen] == ["命令写的.txt"]
