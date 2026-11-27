"""A generated file whose name is taken in My Space is saved as ``name (n).ext``.

Before, the run-end ``persist_artifacts`` raised 409 and the whole chat run
was marked failed ("运行失败：409: 目标文件夹已存在同名文件…").
"""

import pytest
from core.db.models import Artifact, UserShadow
from core.files.artifacts.service import persist_artifacts
from tests.db_support import memory_engine, session_factory


@pytest.fixture
def db():
    session = session_factory(memory_engine())()
    session.add(UserShadow(user_id="alice", user_center_id="alice", username="alice"))
    session.add(Artifact(
        artifact_id="old", user_id="alice", filename="报告.docx", title="报告.docx",
        type="document", mime_type="application/octet-stream", size_bytes=3,
        storage_key="test/old",
    ))
    session.commit()
    yield session
    session.close()


def ref(file_id, name="报告.docx"):
    return {"file_id": file_id, "name": name, "mime_type": "application/octet-stream",
            "size": 3, "storage_key": "test/" + file_id}


@pytest.mark.parametrize("strict", [False, True])
def test_taken_name_is_renamed_not_failed(db, strict):
    persist_artifacts(db, "alice", "chat", [ref("new")], strict=strict)
    row = db.get(Artifact, "new")
    assert (row.filename, row.title) == ("报告 (1).docx", "报告 (1).docx")
    assert db.get(Artifact, "old").filename == "报告.docx"


def test_same_name_twice_in_one_batch_and_run_end_commit(db):
    # The run-end path: caller-owned transaction (commit=False), then commit.
    persist_artifacts(db, "alice", "chat", [ref("a"), ref("b"), ref("c", "其他.txt")], commit=False)
    db.commit()
    names = {r.artifact_id: r.filename for r in db.query(Artifact)}
    assert names == {"old": "报告.docx", "a": "报告 (1).docx", "b": "报告 (2).docx", "c": "其他.txt"}


def test_already_saved_file_is_not_renamed_again(db):
    persist_artifacts(db, "alice", "chat", [ref("new")])
    persist_artifacts(db, "alice", "chat", [ref("new")])  # eager pin, then run end
    assert db.get(Artifact, "new").filename == "报告 (1).docx"
    assert db.query(Artifact).count() == 2


def test_folder_with_that_name_is_also_avoided(db):
    from core.db.models import UserFolder

    db.add(UserFolder(folder_id="f", user_id="alice", parent_folder_id=None, name="数据"))
    db.commit()
    persist_artifacts(db, "alice", "chat", [ref("new", "数据")])
    assert db.get(Artifact, "new").filename == "数据 (1)"
