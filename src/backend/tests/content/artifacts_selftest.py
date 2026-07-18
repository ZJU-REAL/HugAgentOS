"""Selftest: artifact store + /files endpoint."""

from __future__ import annotations

import tempfile
from functools import partial
from pathlib import Path

def main() -> int:
    try:
        import anyio
        from fastapi import HTTPException
        from starlette.responses import FileResponse
        from api.routes.files import download_file
        from api.routes.v1.artifacts import extract_file_ref, resolve_artifact_storage_key
        from core.db.engine import SessionLocal
    except ModuleNotFoundError as e:
        print(f"artifacts_selftest: SKIP (missing dependency: {e})")
        return 0

    import core.artifacts.store as store

    original_store_dir = store._STORE_DIR  # type: ignore[attr-defined]
    original_index_path = store._INDEX_PATH  # type: ignore[attr-defined]

    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "artifacts"
        try:
            store._STORE_DIR = base  # type: ignore[attr-defined]
            store._INDEX_PATH = base / "index.json"  # type: ignore[attr-defined]

            item = store.save_artifact_bytes(
                content=b"demo",
                name="demo.docx",
                mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                extension="docx",
            )
            assert item.get("file_id")
            loaded = store.get_artifact(item["file_id"])
            assert loaded is not None
            if loaded.get("path"):
                assert Path(str(loaded["path"])).exists()
            else:
                assert loaded.get("storage_key") == item["storage_key"]
            assert resolve_artifact_storage_key(item["file_id"], f"artifacts/{item['file_id']}") == item["storage_key"]

            ref = extract_file_ref({
                "file_id": item["file_id"],
                "name": item["name"],
                "mime_type": item["mime_type"],
                "size": item["size"],
                "url": f"/files/{item['file_id']}",
            })
            assert ref is not None
            assert ref["storage_key"] == item["storage_key"]

            with SessionLocal() as db:
                resp = anyio.run(partial(download_file, item["file_id"], db=db, user=None))
                assert isinstance(resp, FileResponse)
                if loaded.get("path"):
                    assert str(resp.path) == str(loaded["path"])
                else:
                    assert Path(str(resp.path)).exists()

                try:
                    anyio.run(partial(download_file, "missing-id", db=db, user=None))
                    raise AssertionError("expected 404 for missing artifact")
                except HTTPException as e:
                    assert e.status_code == 404
        finally:
            store._STORE_DIR = original_store_dir  # type: ignore[attr-defined]
            store._INDEX_PATH = original_index_path  # type: ignore[attr-defined]

    print("artifacts_selftest: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
