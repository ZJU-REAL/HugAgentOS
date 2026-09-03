"""「我的空间」与沙箱镜像目录的实时对账，以及容器交接前清空 /workspace。

回归的是这条真实故障：开了 myspace bind mount 后沙箱的 ``/workspace/myspace/{uid}``
就是后端的 ``myspace_cache/{uid}``，旧回写逻辑拿沙箱文件和镜像缓存比 md5（同一个文件），
恒等为真、一次也没同步出去 —— 文件只存在于磁盘、没有 artifact 记录：用户界面上看不见，
新会话却每次都挂得到。
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.db import models as _models  # noqa: F401 — 让 db_session 建表时认得 artifacts
from core.llm.tools import myspace_mirror as mm


class _FakeDB:
    def close(self) -> None:
        pass


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    """把镜像根目录指到临时目录，DB 会话换成空壳，目录链默认「存在且未删除」。"""
    monkeypatch.setattr(
        "core.llm.tools.myspace_mirror._resolve_chain",
        lambda db, uid, names: mm._Chain(folder_id="f0", exists=True, deleted_ts=None),
    )
    root = tmp_path / "myspace_cache" / "u1"
    root.mkdir(parents=True)
    monkeypatch.setattr(
        "core.sandbox._common.myspace_cache_dir", lambda uid: tmp_path / "myspace_cache" / uid
    )
    monkeypatch.setattr("core.db.engine.SessionLocal", lambda: _FakeDB())
    return root


def _artifact(size: int, updated: datetime, deleted_at=None):
    return SimpleNamespace(
        size_bytes=size, updated_at=updated, created_at=updated, deleted_at=deleted_at
    )


# ── 遍历 ────────────────────────────────────────────────────────────────


def test_iter_mirror_files_returns_relative_paths(mirror):
    (mirror / "大优强全部" / "分片").mkdir(parents=True)
    (mirror / "大优强全部" / "分片" / "a.tsv").write_text("x")
    (mirror / "top.txt").write_text("y")

    rels = sorted(e.rel for e in mm.iter_mirror_files("u1"))
    assert rels == ["top.txt", "大优强全部/分片/a.tsv"]


def test_iter_mirror_files_baseline_diff_ignores_the_clock(mirror):
    """基线差集：只认"快照里没有"或"mtime 变了"的文件，完全不看墙上时钟。

    回归的是真机上抓到的坑：Linux 写文件记的 mtime 取自内核粗粒度时钟，实测比
    time.time() 慢 6~10 毫秒，纯按时间窗口会把命令刚写出来的文件当成旧文件漏掉。
    """
    old = mirror / "没动过.txt"
    old.write_text("x")
    baseline = mm.snapshot_mirror_state("u1")

    (mirror / "新写的.txt").write_text("y")  # 新增
    old.write_text("changed")  # 改动
    rels = sorted(e.rel for e in mm.iter_mirror_files("u1", baseline=baseline))

    assert rels == ["新写的.txt", "没动过.txt"]


def test_iter_mirror_files_since_ts_leaves_slack_for_coarse_mtime(mirror):
    """退化到时间窗口时要为粗粒度时钟让出余量，否则刚写的文件会被判成旧的。"""
    fp = mirror / "刚写的.txt"
    fp.write_text("x")
    just_after = fp.stat().st_mtime + 0.01  # 模拟 time.time() 比 mtime 快几毫秒

    rels = [e.rel for e in mm.iter_mirror_files("u1", since_ts=just_after)]

    assert rels == ["刚写的.txt"]


def test_iter_mirror_files_since_ts_filters_old_files(mirror):
    old = mirror / "old.txt"
    old.write_text("x")
    long_ago = time.time() - 3600
    os.utime(old, (long_ago, long_ago))
    (mirror / "new.txt").write_text("y")

    rels = [e.rel for e in mm.iter_mirror_files("u1", since_ts=time.time() - 60)]
    assert rels == ["new.txt"]


# ── 分类：新文件 vs 改了用户已有文件 vs 用户已删 ──────────────────────────


def test_new_file_is_classified_as_new(mirror, monkeypatch):
    """真实故障的回归：文件就在镜像目录里、没有登记记录 → 要登记展示。"""
    (mirror / "分片").mkdir()
    (mirror / "分片" / "确认结果.tsv").write_text("a\tb\n")
    monkeypatch.setattr(mm, "_artifact_in", lambda db, uid, fid, name: None)

    changes = mm.collect_mirror_changes(user_id="u1")

    assert [e.logical_path for e in changes.new] == ["/myspace/分片/确认结果.tsv"]
    assert changes.modified == []


def test_touching_an_existing_user_file_is_classified_as_modified(mirror, monkeypatch):
    """改的是用户已有的文件 → 归入 modified，由调用方过确认门。"""
    fp = mirror / "报告.docx"
    fp.write_bytes(b"changed")
    art = _artifact(3, datetime.now(timezone.utc) - timedelta(minutes=10))
    monkeypatch.setattr(mm, "_artifact_in", lambda db, uid, fid, name: art)

    changes = mm.collect_mirror_changes(user_id="u1")

    assert changes.new == []
    assert [e.rel for e in changes.modified] == ["报告.docx"]


def test_already_registered_file_is_skipped(mirror, monkeypatch):
    fp = mirror / "a.txt"
    fp.write_text("hello")
    art = _artifact(fp.stat().st_size, datetime.now(timezone.utc) + timedelta(seconds=5))
    monkeypatch.setattr(mm, "_artifact_in", lambda db, uid, fid, name: art)

    changes = mm.collect_mirror_changes(user_id="u1")

    assert (changes.new, changes.modified, changes.skipped_current) == ([], [], 1)


def test_file_deleted_by_the_user_is_never_resurrected(mirror, monkeypatch):
    """用户在界面上删掉的文件，镜像里还留着副本 → 登记它等于把删除撤销。"""
    (mirror / "已删.tsv").write_bytes(b"x")
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        mm, "_artifact_in", lambda db, uid, fid, name: _artifact(1, now, deleted_at=now)
    )

    changes = mm.collect_mirror_changes(user_id="u1")

    assert (changes.new, changes.modified, changes.skipped_deleted) == ([], [], 1)


def test_file_rewritten_after_the_deletion_counts_as_new(mirror, monkeypatch):
    """删除之后沙箱又写了同名文件 → 那是新内容，照常登记。"""
    (mirror / "又写了.tsv").write_bytes(b"new")
    long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    monkeypatch.setattr(
        mm, "_artifact_in",
        lambda db, uid, fid, name: _artifact(1, long_ago, deleted_at=long_ago),
    )

    changes = mm.collect_mirror_changes(user_id="u1")

    assert [e.rel for e in changes.new] == ["又写了.tsv"]


def test_file_under_a_folder_the_user_deleted_is_never_registered(mirror, monkeypatch):
    """整个文件夹被用户删掉，里面的文件从没登记过 —— 登记它们会连文件夹一起复活。

    生产上真实发生过：某账号删了「大优强全部」文件夹，镜像里 2000 多个文件仍在，若按
    "没有登记记录 = 新文件"处理，2 GB 内容会在用户刚清理完之后原样回到他的空间。
    """
    (mirror / "大优强全部" / "分片").mkdir(parents=True)
    (mirror / "大优强全部" / "分片" / "a.tsv").write_text("x")
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        mm, "_resolve_chain",
        lambda db, uid, names: mm._Chain(
            folder_id="f1", exists=True, deleted_ts=now.timestamp() + 5
        ),
    )
    monkeypatch.setattr(mm, "_artifact_in", lambda db, uid, fid, name: None)

    changes = mm.collect_mirror_changes(user_id="u1")

    assert (changes.new, changes.modified) == ([], [])
    assert [e.rel for e in changes.stale] == ["大优强全部/分片/a.tsv"]


def test_file_written_after_the_folder_was_deleted_is_new(mirror, monkeypatch):
    """文件夹删掉之后沙箱又写了文件 → 那是新内容，照常登记。"""
    (mirror / "大优强全部").mkdir()
    (mirror / "大优强全部" / "new.tsv").write_text("x")
    long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    monkeypatch.setattr(
        mm, "_resolve_chain",
        lambda db, uid, names: mm._Chain(
            folder_id="f1", exists=True, deleted_ts=long_ago.timestamp()
        ),
    )
    monkeypatch.setattr(mm, "_artifact_in", lambda db, uid, fid, name: None)

    changes = mm.collect_mirror_changes(user_id="u1")

    assert [e.rel for e in changes.new] == ["大优强全部/new.tsv"]


def test_recency_not_size_decides_the_direction(mirror, monkeypatch):
    """用户在界面上重传了同名文件（登记更新、镜像还旧）→ 不许被镜像里的旧副本盖掉。"""
    fp = mirror / "报告.docx"
    fp.write_bytes(b"old")
    art = _artifact(999, datetime.now(timezone.utc) + timedelta(minutes=5))
    monkeypatch.setattr(mm, "_artifact_in", lambda db, uid, fid, name: art)

    changes = mm.collect_mirror_changes(user_id="u1")

    assert (changes.new, changes.modified, changes.skipped_current) == ([], [], 1)


def test_oversized_file_is_skipped(mirror, monkeypatch):
    (mirror / "big.bin").write_bytes(b"0" * 100)
    monkeypatch.setattr(mm, "_artifact_in", lambda db, uid, fid, name: None)

    changes = mm.collect_mirror_changes(user_id="u1", max_bytes=10)

    assert (changes.skipped_too_large, changes.new) == (1, [])


def test_collect_writes_nothing(mirror, monkeypatch):
    """分类只判断、不落库 —— 这是它能放进 HTTP 读路径的前提。"""
    (mirror / "a.txt").write_text("hello")
    monkeypatch.setattr(mm, "_artifact_in", lambda db, uid, fid, name: None)
    monkeypatch.setattr(mm._ms, "sync_upsert", lambda **kw: pytest.fail("分类阶段不得写库"))

    assert len(mm.collect_mirror_changes(user_id="u1").new) == 1


# ── 正向：我的空间 → 镜像 ─────────────────────────────────────────────────


class _FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *_a):
        return self

    def all(self):
        return self.rows


def _pull_db(rows, monkeypatch, downloads=b"new"):
    class _DB:
        def query(self, _model):
            return _FakeQuery(rows)

        def close(self):
            pass

    monkeypatch.setattr("core.db.engine.SessionLocal", lambda: _DB())
    monkeypatch.setattr(
        "core.storage.get_storage",
        lambda: SimpleNamespace(download_bytes=lambda key: downloads),
    )
    mm.reset_pull_cursor("u1")


def _row(filename, *, size, updated, deleted_at=None):
    return SimpleNamespace(
        filename=filename,
        user_folder_id=None,
        size_bytes=size,
        updated_at=updated,
        created_at=updated,
        deleted_at=deleted_at,
        storage_key=f"k/{filename}",
    )


def test_pull_materializes_a_file_missing_from_the_mirror(mirror, monkeypatch):
    """用户刚在界面上传的文件，沙箱要立刻看得到。"""
    _pull_db([_row("上传.xlsx", size=3, updated=datetime.now(timezone.utc))], monkeypatch)

    rep = mm.pull_myspace_updates(user_id="u1")

    assert rep.materialized == 1
    assert (mirror / "上传.xlsx").read_bytes() == b"new"


def test_pull_leaves_a_newer_mirror_file_alone(mirror, monkeypatch):
    """沙箱刚写过的文件比登记版本新 → 留给反向对账，不能被旧内容盖掉。"""
    fp = mirror / "分片.tsv"
    fp.write_bytes(b"sandbox-wrote-this")
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    _pull_db([_row("分片.tsv", size=2, updated=old)], monkeypatch)

    rep = mm.pull_myspace_updates(user_id="u1")

    assert rep.materialized == 0
    assert fp.read_bytes() == b"sandbox-wrote-this"


def test_pull_propagates_deletions_to_the_mirror(mirror, monkeypatch):
    """用户删掉的文件，镜像副本必须一起消失，否则新沙箱照样挂得到。"""
    gone = mirror / "已删.tsv"
    gone.write_bytes(b"x")
    now = datetime.now(timezone.utc)
    _pull_db([_row("已删.tsv", size=1, updated=now, deleted_at=now)], monkeypatch)

    rep = mm.pull_myspace_updates(user_id="u1")

    assert rep.removed == 1
    assert not gone.exists()


def test_pull_never_touches_files_without_an_artifact_row(mirror, monkeypatch):
    """没有登记记录的文件是等着登记的新文件，删除传导绝不能碰它们。"""
    stray = mirror / "待登记.tsv"
    stray.write_bytes(b"x")
    _pull_db([], monkeypatch)

    rep = mm.pull_myspace_updates(user_id="u1")

    assert rep.removed == 0
    assert stray.exists()


def test_pull_keeps_a_file_rewritten_after_the_deletion(mirror, monkeypatch):
    fresh = mirror / "又写了.tsv"
    fresh.write_bytes(b"new content")
    long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    _pull_db(
        [_row("又写了.tsv", size=1, updated=long_ago, deleted_at=long_ago)], monkeypatch
    )

    rep = mm.pull_myspace_updates(user_id="u1")

    assert rep.removed == 0
    assert fresh.exists()


def test_reconcile_on_read_is_incremental_after_the_first_pass(mirror, monkeypatch):
    """读时对账挂在会被轮询的列表接口上，第二次起只看新落盘的文件。"""
    (mirror / "a.txt").write_text("x")
    scanned: list = []

    def _collect(**kw):
        scanned.append(kw.get("since_ts"))
        return mm.MirrorChanges()

    monkeypatch.setattr(mm, "collect_mirror_changes", _collect)
    monkeypatch.setattr(mm, "pull_myspace_updates", lambda **kw: mm.PullReport())
    mm.reset_pull_cursor("u1")

    mm.reconcile_on_read(user_id="u1")
    mm.reconcile_on_read(user_id="u1")

    assert scanned[0] is None  # 首轮全量，把存量欠账捞出来
    assert scanned[1] is not None and scanned[1] > 0  # 之后按水位增量


def test_pull_catches_a_deletion_that_did_not_touch_updated_at(mirror, monkeypatch, db_session):
    """界面删除只写 deleted_at、不动 updated_at（见 ArtifactRepository.soft_delete_owned）。

    增量同步若只看 updated_at，这类删除会整批漏掉：镜像里的副本留着，沙箱照样看得见。
    这里用真数据库跑一遍 SQL，确保过滤条件真的把它捞出来了。
    """
    from datetime import datetime as _dt

    from core.db.models import Artifact

    gone = mirror / "界面上删掉的.txt"
    gone.write_bytes(b"x")
    long_ago = _dt(2020, 1, 1, tzinfo=timezone.utc)
    db_session.add(
        Artifact(
            artifact_id="a-deleted",
            user_id="u1",
            filename="界面上删掉的.txt",
            title="界面上删掉的.txt",
            type="other",
            mime_type="text/plain",
            storage_key="k/1",
            size_bytes=1,
            created_at=long_ago,
            updated_at=long_ago,  # 删除时没有被更新
            deleted_at=_dt.now(timezone.utc),
        )
    )
    db_session.commit()

    monkeypatch.setattr("core.db.engine.SessionLocal", lambda: db_session)
    monkeypatch.setattr(
        "core.storage.get_storage",
        lambda: SimpleNamespace(download_bytes=lambda key: b""),
    )
    # 直接置成增量水位：首轮是全量扫描，捞得到删除是理所当然的；要验的是增量这条路
    mm._pull_cursor["u1"] = _dt.now(timezone.utc).timestamp() - 60

    rep = mm.pull_myspace_updates(user_id="u1")

    assert rep.removed == 1
    assert not gone.exists()


# ── bash 两侧对账 ────────────────────────────────────────────────────────


def _bash_tool(**kwargs):
    from core.llm.tools.sandbox_tool import register_bash

    class _Toolkit:
        fn = None

        def register_tool_function(self, fn, **_kw):
            self.fn = fn

    tk = _Toolkit()
    register_bash(tk, loader=None, loaded_skill_ids=set(), chat_id="chat-1", **kwargs)
    assert tk.fn is not None
    return tk.fn


@pytest.mark.parametrize(
    "command,exit_code",
    [
        # 路径由变量拼出，命令文本里根本没有 "myspace" 字样
        ('cd "$WORK" && python3 gen.py > 分片/a.tsv', 0),
        # 命令最终失败，但失败之前已经写出了文件
        ("python3 gen.py && exit 1", 1),
    ],
)
def test_bash_reconciles_regardless_of_command_text_and_exit_code(
    monkeypatch, command, exit_code
):
    import core.llm.tools.sandbox_tool as st

    monkeypatch.setenv("SANDBOX_TOOLS_ENABLED", "true")
    seen: dict = {}

    async def _fake_execute(req):
        return SimpleNamespace(
            stdout="", stderr="", exit_code=exit_code, execution_time_ms=1, files=[]
        )

    monkeypatch.setattr(
        "core.sandbox.get_sandbox_provider",
        lambda: SimpleNamespace(execute=_fake_execute, myspace_mirror_live=True),
    )

    async def _fake_pull(user_id):
        seen["pulled"] = user_id

    async def _fake_sync(*, sess, user_id, chat_id, interactive, since_ts, mirror_before):
        seen["synced"] = (user_id, since_ts)
        return [], [], []

    monkeypatch.setattr(st, "_pull_myspace_updates", _fake_pull)
    monkeypatch.setattr(st, "_sync_myspace_changes", _fake_sync)

    tool = _bash_tool(user_id="u1")
    before = time.time()
    asyncio.run(tool(command=command))

    assert seen["pulled"] == "u1"
    assert seen["synced"][0] == "u1"
    # 对账窗口从命令开始那一刻算起，跑很久的长命令也不会漏
    assert seen["synced"][1] >= before


def _sync_with(monkeypatch, *, new, modified, interactive=True, guard=None, patch_guard=True):
    import core.llm.tools.sandbox_tool as st

    monkeypatch.setattr(
        "core.sandbox.get_sandbox_provider",
        lambda: SimpleNamespace(myspace_mirror_live=True),
    )
    monkeypatch.setattr(
        mm, "collect_mirror_changes",
        lambda **kw: mm.MirrorChanges(new=list(new), modified=list(modified)),
    )
    registered: list[str] = []

    def _register(*, user_id, chat_id, entry):
        registered.append(entry.rel)
        return {"file_id": entry.rel, "in_place_update": False}

    monkeypatch.setattr(mm, "register_entry", _register)
    monkeypatch.setattr(mm, "deleted_since", lambda **kw: [])
    monkeypatch.setattr(st, "pin_artifact_to_workspace", lambda ref: None, raising=False)
    monkeypatch.setattr(
        "core.llm.tools._common.pin_artifact_to_workspace", lambda ref: None
    )

    if patch_guard and interactive:
        async def _guard(**kw):
            return guard

        monkeypatch.setattr("core.llm.tools._common.myspace_write_guard", _guard)
    synced, blocked, _deleted = asyncio.run(
        st._sync_myspace_changes(
            sess="chat-1",
            user_id="u1",
            chat_id="chat-1",
            interactive=interactive,
            since_ts=0.0,
        )
    )
    return registered, synced, blocked


def _entry(rel):
    return mm.MirrorEntry(rel=rel, path=Path("/tmp") / rel, size=1, mtime=time.time())


def test_new_files_register_without_a_confirmation_prompt(monkeypatch):
    """写在 /myspace 下的新文件就是用户的文件，直接登记展示，不打断用户。"""
    registered, synced, blocked = _sync_with(
        monkeypatch, new=[_entry("分片/a.tsv"), _entry("分片/b.tsv")], modified=[]
    )

    assert sorted(registered) == ["分片/a.tsv", "分片/b.tsv"]  # 并发登记，不保证顺序
    assert len(synced) == 2 and blocked == []


def test_modifying_an_existing_user_file_goes_through_the_confirmation_gate(monkeypatch):
    """覆盖用户已有的文件仍然要用户点头 —— 确认门被拦下就不写。"""
    registered, synced, blocked = _sync_with(
        monkeypatch, new=[], modified=[_entry("报告.docx")], guard={"status": "blocked"}
    )

    assert registered == []
    assert synced == [] and blocked == ["/myspace/报告.docx"]


def test_subagent_modification_takes_effect_without_asking(monkeypatch):
    """子智能体/批量/定时任务问不到人 —— 直接生效，不能拒绝。

    拒绝拦不住已经发生的改动（文件在沙箱里早就改了），只会让界面停在旧内容上。
    """
    def _must_not_ask(**kw):
        pytest.fail("非交互场景不该走确认门")

    monkeypatch.setattr("core.llm.tools._common.myspace_write_guard", _must_not_ask)
    registered, synced, blocked = _sync_with(
        monkeypatch, new=[], modified=[_entry("报告.docx")], interactive=False
    )

    assert registered == ["报告.docx"]
    assert len(synced) == 1 and blocked == []


def test_subagent_deletion_takes_effect_without_asking(monkeypatch):
    def _must_not_ask(**kw):
        pytest.fail("非交互场景不该走确认门")

    monkeypatch.setattr("core.llm.tools._common.myspace_write_guard", _must_not_ask)
    import core.llm.tools.sandbox_tool as st

    monkeypatch.setattr(
        "core.sandbox.get_sandbox_provider",
        lambda: SimpleNamespace(myspace_mirror_live=True),
    )
    monkeypatch.setattr(mm, "collect_mirror_changes", lambda **kw: mm.MirrorChanges())
    monkeypatch.setattr(mm, "deleted_since", lambda **kw: ["分片/a.tsv"])
    removed: list = []
    monkeypatch.setattr(
        mm, "delete_registered", lambda *, user_id, rel: (removed.append(rel), True)[1]
    )

    _synced, blocked, deleted = asyncio.run(
        st._sync_myspace_changes(
            sess="chat-1",
            user_id="u1",
            chat_id="chat-1",
            interactive=False,
            since_ts=0.0,
            mirror_before={"分片/a.tsv": (1.0, 1)},
        )
    )

    assert removed == ["分片/a.tsv"]
    assert deleted == ["/myspace/分片/a.tsv"] and blocked == []


def test_approved_modification_is_written_back(monkeypatch):
    registered, synced, blocked = _sync_with(
        monkeypatch, new=[], modified=[_entry("报告.docx")], guard=None
    )

    assert registered == ["报告.docx"]
    assert len(synced) == 1 and blocked == []


# ── 删除：沙箱 rm 掉的文件要同步删掉「我的空间」里的 ──────────────────────


def test_deleted_since_reports_only_registered_files(mirror, monkeypatch):
    """快照里有、现在没了、且仍在册的才要同步删；没登记过的消失了就消失了。"""
    (mirror / "留着.tsv").write_text("x")
    art = _artifact(1, datetime.now(timezone.utc))
    monkeypatch.setattr(
        mm, "_artifact_in",
        lambda db, uid, fid, name: art if name == "已登记.tsv" else None,
    )

    rels = mm.deleted_since(
        user_id="u1",
        snapshot={"留着.tsv": (1.0, 1), "已登记.tsv": (1.0, 1), "没登记过.tsv": (1.0, 1)},
    )

    assert rels == ["已登记.tsv"]


def test_deleted_since_ignores_an_empty_snapshot(mirror):
    """拿不到快照时退化成"不做删除同步"，绝不能把整个空间判成被删。"""
    assert mm.deleted_since(user_id="u1", snapshot={}) == []


def _sync_deletes(monkeypatch, *, missing, guard=None):
    import core.llm.tools.sandbox_tool as st

    monkeypatch.setattr(
        "core.sandbox.get_sandbox_provider",
        lambda: SimpleNamespace(myspace_mirror_live=True),
    )
    monkeypatch.setattr(mm, "collect_mirror_changes", lambda **kw: mm.MirrorChanges())
    monkeypatch.setattr(mm, "deleted_since", lambda **kw: list(missing))
    removed: list[str] = []
    monkeypatch.setattr(
        mm, "delete_registered",
        lambda *, user_id, rel: (removed.append(rel), True)[1],
    )

    async def _guard(**kw):
        return guard

    monkeypatch.setattr("core.llm.tools._common.myspace_write_guard", _guard)
    _synced, blocked, deleted = asyncio.run(
        st._sync_myspace_changes(
            sess="chat-1",
            user_id="u1",
            chat_id="chat-1",
            interactive=True,
            since_ts=0.0,
            mirror_before={"分片/a.tsv": (1.0, 1)},
        )
    )
    return removed, blocked, deleted


def test_rm_in_the_sandbox_removes_the_file_from_myspace(monkeypatch):
    removed, blocked, deleted = _sync_deletes(monkeypatch, missing=["分片/a.tsv"])

    assert removed == ["分片/a.tsv"]
    assert deleted == ["/myspace/分片/a.tsv"] and blocked == []


def test_a_refused_deletion_leaves_myspace_untouched(monkeypatch):
    """确认门拦下就不删；文件会被下一条命令的正向同步拉回镜像，两边仍然一致。"""
    removed, blocked, deleted = _sync_deletes(
        monkeypatch, missing=["分片/a.tsv"], guard={"status": "blocked"}
    )

    assert removed == [] and deleted == []
    assert blocked == ["/myspace/分片/a.tsv"]


# ── 界面上改名/移动：镜像里的旧路径要跟着清掉 ──────────────────────────────


def test_folder_rel_walks_up_the_parent_chain(monkeypatch, tmp_path):
    """旧路径要按改动**之前**的目录链算，算错就清错目录。"""
    from core.services.user_folder_service import UserFolderService

    svc = UserFolderService.__new__(UserFolderService)
    rows = {
        "f1": SimpleNamespace(name="大优强全部", parent_folder_id=None, folder_id="f1"),
        "f2": SimpleNamespace(name="分片", parent_folder_id="f1", folder_id="f2"),
    }
    monkeypatch.setattr(UserFolderService, "get", lambda self, fid: rows.get(fid))

    assert svc._folder_rel(rows["f2"]) == "大优强全部/分片"


# ── 容器交接前清空 /workspace ─────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _execd_opts_stub(monkeypatch):
    """本机没装 opensandbox SDK，补一个 RunCommandOpts 存根（线上是真 SDK）。"""
    import sys
    import types

    if "opensandbox.models.execd" in sys.modules:
        return
    mod = types.ModuleType("opensandbox.models.execd")
    mod.RunCommandOpts = lambda **kw: SimpleNamespace(**kw)
    monkeypatch.setitem(sys.modules, "opensandbox", types.ModuleType("opensandbox"))
    monkeypatch.setitem(sys.modules, "opensandbox.models", types.ModuleType("opensandbox.models"))
    monkeypatch.setitem(sys.modules, "opensandbox.models.execd", mod)


class _FakeCommands:
    def __init__(self, exit_code=0, boom=False):
        self.exit_code = exit_code
        self.boom = boom
        self.cmd = None

    async def run(self, cmd, opts=None):
        self.cmd = cmd
        if self.boom:
            raise RuntimeError("execd 失联")
        return SimpleNamespace(exit_code=self.exit_code)


def _session_with(commands):
    from core.sandbox._opensandbox_internals import _Session

    return _Session(
        sandbox=SimpleNamespace(id="sbx-1", commands=commands),
        interpreter=None,
        contexts={},
        seeded_myspace_mtime={},
        user_id="u1",
        pool_source="user",
    )


def _mixin():
    from core.sandbox._opensandbox_session import _OpenSandboxSessionMixin

    return _OpenSandboxSessionMixin()


def test_wipe_workspace_keeps_mounts_and_rebuilds_scratch():
    cmds = _FakeCommands()
    assert asyncio.run(_mixin()._wipe_workspace(_session_with(cmds))) is True
    assert "/workspace -mindepth 1 -maxdepth 1" in cmds.cmd
    assert "! -name myspace" in cmds.cmd
    assert "! -name skills" in cmds.cmd
    assert "/workspace/scratch" in cmds.cmd


def test_wipe_workspace_failure_blocks_reuse():
    """擦不干净就不复用 —— 宁可重建，也不把脏容器递给下一个会话。"""
    assert asyncio.run(_mixin()._wipe_workspace(_session_with(_FakeCommands(exit_code=1)))) is False
    assert asyncio.run(_mixin()._wipe_workspace(_session_with(_FakeCommands(boom=True)))) is False
