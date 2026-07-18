"""Selftest: UserFolderService — key-path verification mirroring TeamFolderService.

How to run: PYTHONPATH=src/backend python -m tests.user_folder_service_selftest
or run automatically as part of make selftest.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


def main() -> int:
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from core.db.engine import Base
        from core.db.models import Artifact, UserFolder, UserShadow
        from core.services.user_folder_service import (
            MAX_FOLDER_DEPTH,
            UserFolderService,
            _sanitize_name,
        )
    except ModuleNotFoundError as e:
        print(f"user_folder_service_selftest: SKIP (missing dependency: {e})")
        return 0

    # ── In-memory SQLite isolated environment ──
    with tempfile.TemporaryDirectory() as td:
        url = f"sqlite:///{Path(td) / 'test.db'}"
        engine = create_engine(url)
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)
        db = SessionLocal()

        try:
            uid = "u_test_1"
            db.add(UserShadow(user_id=uid, username="t1"))
            db.commit()

            svc = UserFolderService(db)

            # ── _sanitize_name ──
            assert _sanitize_name("")[0] is False
            assert _sanitize_name("  ")[0] is False
            assert _sanitize_name("a/b")[0] is False
            assert _sanitize_name(".")[0] is False
            assert _sanitize_name("..")[0] is False
            assert _sanitize_name("x" * 256)[0] is False
            assert _sanitize_name(" hello ")[1] == "hello"

            # ── create root folder ──
            r1 = svc.create_folder(uid, None, "工作", actor=uid)
            assert r1.ok, r1.message
            f1 = r1.folder_id
            assert f1 and f1.startswith("ufld_")

            # reject same-name at the same level
            r_dup = svc.create_folder(uid, None, "工作", actor=uid)
            assert not r_dup.ok and "同名" in r_dup.message

            # subfolder
            r2 = svc.create_folder(uid, f1, "项目A", actor=uid)
            assert r2.ok
            f2 = r2.folder_id

            # cross-user parent validation
            other = "u_other"
            db.add(UserShadow(user_id=other, username="o"))
            db.commit()
            r_x = svc.create_folder(other, f1, "x", actor=other)
            assert not r_x.ok and "不属于" in r_x.message

            # ── tree ──
            tree = svc.get_tree(uid)
            assert len(tree) == 1
            assert tree[0]["name"] == "工作"
            assert tree[0]["children"][0]["name"] == "项目A"

            # ── breadcrumb ──
            bc = svc.get_breadcrumb(f2, uid)
            assert [b["name"] for b in bc] == ["工作", "项目A"]
            # cross-user breadcrumb should be empty
            assert svc.get_breadcrumb(f2, other) == []

            # ── rename ──
            r_re = svc.rename_folder(f2, "项目B", actor=uid)
            assert r_re.ok
            assert svc.get(f2).name == "项目B"

            # reject cross-user rename
            assert not svc.rename_folder(f2, "x", actor=other).ok

            # ── move ──
            r3 = svc.create_folder(uid, None, "归档", actor=uid)
            f3 = r3.folder_id
            r_mv = svc.move_folder(f2, f3, actor=uid)
            assert r_mv.ok
            assert svc.get(f2).parent_folder_id == f3

            # cannot move into itself/its descendants
            assert not svc.move_folder(f3, f2, actor=uid).ok

            # ── depth limit ──
            cur_parent = None
            chain_ids = []
            for i in range(MAX_FOLDER_DEPTH):
                rr = svc.create_folder(uid, cur_parent, f"L{i}", actor=uid)
                assert rr.ok, f"depth {i} failed: {rr.message}"
                cur_parent = rr.folder_id
                chain_ids.append(cur_parent)
            # the MAX_FOLDER_DEPTH+1 th level should be rejected
            rr_over = svc.create_folder(uid, cur_parent, "TooDeep", actor=uid)
            assert not rr_over.ok and "层级" in rr_over.message

            # ── cascade soft delete ──
            # attach an artifact under f1
            db.add(Artifact(
                artifact_id="a1",
                user_id=uid,
                user_folder_id=f1,
                type="document",
                title="t",
                filename="t.txt",
                size_bytes=1,
                mime_type="text/plain",
                storage_key="k",
            ))
            # also attach one under f1's child (already moved to f3)
            db.add(Artifact(
                artifact_id="a2",
                user_id=uid,
                user_folder_id=f2,  # f2 already moved under f3
                type="document",
                title="t2",
                filename="t2.txt",
                size_bytes=1,
                mime_type="text/plain",
                storage_key="k2",
            ))
            db.commit()

            cnt_f1 = svc.count_affected_artifacts(f1, uid)
            assert cnt_f1 == 1, f"expected 1 affected under f1 (a1), got {cnt_f1}"

            cnt_f3 = svc.count_affected_artifacts(f3, uid)
            assert cnt_f3 == 1, f"expected 1 affected under f3 (a2 via f2), got {cnt_f3}"

            res, n = svc.delete_folder(f3, actor=uid)
            assert res.ok and n == 1
            assert svc.get(f3) is None
            assert svc.get(f2) is None  # child is also cascade soft-deleted
            a2 = db.query(Artifact).filter(Artifact.artifact_id == "a2").first()
            assert a2.deleted_at is not None

            # ── move artifact ──
            r_mv_a = svc.move_artifact("a1", None, actor=uid)
            assert r_mv_a.ok
            db.refresh(db.query(Artifact).filter(Artifact.artifact_id == "a1").first())
            a1 = db.query(Artifact).filter(Artifact.artifact_id == "a1").first()
            assert a1.user_folder_id is None

            # reject cross-user move
            assert not svc.move_artifact("a1", None, actor=other).ok

            print("user_folder_service_selftest: PASS")
            return 0
        finally:
            db.close()
            Base.metadata.drop_all(engine)


if __name__ == "__main__":
    sys.exit(main())
