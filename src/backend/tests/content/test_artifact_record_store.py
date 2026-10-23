"""生成物登记表：一条记录一个文件，读写代价与装机规模无关。

原来是一本 ``index.json`` 装下所有记录。新登记一个文件要把整本重写一遍、再整份传一次
对象存储；每查一个文件又要把整本重读重解析一遍。生产上这本账 12.9MB / 26359 条，两头
的代价都随装机量一起涨，而且都压在请求路径上——一次「我的空间」首开查了 777 次，光解析
就 112 秒，把整个后端冻住。

记录彼此独立、正常运行中从不需要整表遍历，所以拆成一条一个文件，读写恒定为 O(1)。
"""

import json

import pytest
from core.files.artifacts import store


@pytest.fixture
def store_dir(tmp_path, monkeypatch):
    # Every other path derives from _STORE_DIR, so this one patch is the whole redirect.
    monkeypatch.setattr(store, "_STORE_DIR", tmp_path)
    monkeypatch.setattr(store.process_state(), 'known_buckets', set())
    monkeypatch.setenv("STORAGE_TYPE", "local")
    return tmp_path


def _item(file_id, **over):
    item = {
        "file_id": file_id,
        "name": f"{file_id}.docx",
        "mime_type": "application/octet-stream",
        "size": 10,
        "path": None,
        "storage_key": f"artifacts/{file_id}.docx",
        "created_at": "2026-01-01T00:00:00",
        "metadata": {},
    }
    item.update(over)
    return item


def test_register_then_read_back(store_dir):
    store._record_artifact(_item("abcd1234"))

    got = store.get_artifact("abcd1234")

    assert got["storage_key"] == "artifacts/abcd1234.docx"


def test_one_write_touches_one_file(store_dir):
    store._record_artifact(_item("aa11"))
    store._record_artifact(_item("bb22"))

    # 写第二条不得改动第一条所在的文件——这正是"整本重写"要根治的地方
    first = store._record_path("aa11")
    before = first.stat().st_mtime_ns
    store._record_artifact(_item("cc33"))

    assert first.stat().st_mtime_ns == before
    assert store.get_artifact("bb22") is not None


def test_unknown_id_is_none(store_dir):
    assert store.get_artifact("nope") is None
    assert store.get_artifact("") is None


def test_migration_moves_every_legacy_entry(store_dir):
    legacy = {"files": {f"id{i:03d}": _item(f"id{i:03d}") for i in range(50)}}
    (store_dir / "index.json").write_text(json.dumps(legacy), encoding="utf-8")

    written = store.migrate_legacy_index()

    assert written == 50
    for i in range(50):
        assert store.get_artifact(f"id{i:03d}")["name"] == f"id{i:03d}.docx"
    # 旧文件留档而不是删掉，迁移出问题时还能回头核对
    assert (store_dir / "index.json.migrated").exists()
    assert not (store_dir / "index.json").exists()


def test_migration_is_idempotent_and_resumable(store_dir):
    legacy = {"files": {f"id{i}": _item(f"id{i}") for i in range(5)}}
    (store_dir / "index.json").write_text(json.dumps(legacy), encoding="utf-8")
    # 模拟上一轮迁移中断：已经写出去 2 条
    store._record_artifact(_item("id0"))
    store._record_artifact(_item("id1"))

    assert store.migrate_legacy_index() == 3
    assert store.migrate_legacy_index() == 0
    for i in range(5):
        assert store.get_artifact(f"id{i}") is not None


def test_migration_keeps_legacy_file_when_unreadable(store_dir):
    (store_dir / "index.json").write_text("{ not json", encoding="utf-8")

    assert store.migrate_legacy_index() == 0
    # 读不懂就不许当成"迁移完了"，否则记录就真丢了
    assert (store_dir / "index.json").exists()
    assert not (store_dir / "index.json.migrated").exists()


def test_partially_written_record_is_never_served(store_dir):
    store._record_artifact(_item("dd44"))
    path = store._record_path("dd44")
    path.write_text('{"file_id": "dd44", "sto', encoding="utf-8")

    assert store.get_artifact("dd44") is None
