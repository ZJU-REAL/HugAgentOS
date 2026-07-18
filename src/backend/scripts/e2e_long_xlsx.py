"""End-to-end test: long xlsx → batch_plan → orchestrator iteration.

Validates the fix for batch execution truncation:

  Test A: parse_xlsx_preview produces hook-ready preview with real total_rows
  Test B: _build_xlsx_preview_block (file_context_hook) produces correct block
  Test C: _resolve_xlsx_items (batch planner) keeps ALL 1114 rows, not 200
  Test D: BatchOrchestrator._run_until_done iterates through every row
          (using a mock per-item executor — no real LLM calls)

Run inside the backend container:
  docker exec hugagent-backend bash -c "cd /app && PYTHONPATH=src/backend python src/backend/scripts/e2e_long_xlsx.py /tmp/test_long.xlsx"
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from typing import List, Tuple

# ---------------------------------------------------------------------------
# Setup: insert artifact + plan into DB so the rest of the pipeline can read it
# ---------------------------------------------------------------------------


def _resolve_test_user() -> str:
    """Return any existing user_id from users_shadow (FK target).

    The FK constraint on artifacts.user_id requires a real user; we don't
    actually exercise auth in this script so any row will do.
    """
    from sqlalchemy import text
    from core.db.engine import SessionLocal

    with SessionLocal() as db:
        row = db.execute(text("SELECT user_id FROM users_shadow LIMIT 1")).first()
        if not row:
            raise RuntimeError("users_shadow is empty — log in via the frontend once first")
        return row[0]


def setup_artifact(file_path: str, user_id: str) -> str:
    """Upload file to storage, create Artifact row, return file_id."""
    from core.db.engine import SessionLocal
    from core.db.models import Artifact
    from core.storage import get_storage

    with open(file_path, "rb") as f:
        file_bytes = f.read()

    artifact_id = f"ua_{uuid.uuid4().hex[:16]}"
    storage_key = f"test/{user_id}/user_uploads/{artifact_id}/test.xlsx"

    storage = get_storage()
    storage.upload_bytes(file_bytes, storage_key)

    with SessionLocal() as db:
        art = Artifact(
            artifact_id=artifact_id,
            user_id=user_id,
            type="other",
            title="编办内部材料.xlsx",
            filename="编办内部材料.xlsx",
            size_bytes=len(file_bytes),
            mime_type=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            storage_key=storage_key,
            extra_data={"source": "user_upload"},
        )
        db.add(art)
        db.commit()
    return artifact_id


def cleanup_artifact(file_id: str) -> None:
    """Best-effort: remove artifact row + associated plan rows."""
    from core.db.engine import SessionLocal
    from core.db.models import Artifact, BatchPlan

    with SessionLocal() as db:
        db.query(BatchPlan).filter(
            BatchPlan.items != None  # noqa: E711 — JSONB filter
        ).all()
        db.query(Artifact).filter(Artifact.artifact_id == file_id).delete()
        db.commit()


# ---------------------------------------------------------------------------
# Test A — parse_xlsx_preview
# ---------------------------------------------------------------------------


def test_a_preview(file_path: str) -> Tuple[bool, str]:
    from core.content.file_parser import parse_xlsx_preview

    with open(file_path, "rb") as f:
        bs = f.read()
    info = parse_xlsx_preview(bs, char_budget=50_000)

    expected = {
        "total_rows": 1114,
        "total_columns": 17,
    }
    if info["total_rows"] != expected["total_rows"]:
        return False, f"total_rows={info['total_rows']} != {expected['total_rows']}"
    if info["total_columns"] != expected["total_columns"]:
        return False, f"total_columns={info['total_columns']} != {expected['total_columns']}"
    preview_chars = len(info["preview_md"])
    if preview_chars > 50_000 + 200:
        return False, f"preview_chars={preview_chars} exceeds budget"
    if info["preview_rows"] < 10:
        return False, f"preview_rows={info['preview_rows']} suspiciously small"
    return True, (
        f"total_rows={info['total_rows']} preview_rows={info['preview_rows']} "
        f"preview_chars={preview_chars}"
    )


# ---------------------------------------------------------------------------
# Test B — file_context hook block
# ---------------------------------------------------------------------------


def test_b_hook_block(file_id: str) -> Tuple[bool, str]:
    from core.llm.hooks import _build_xlsx_preview_block

    block = _build_xlsx_preview_block({
        "file_id": file_id,
        "name": "编办内部材料.xlsx",
        "mime_type": (
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
    })
    if block is None:
        return False, "block is None — storage download failed?"
    checks = [
        "总规模: 1114 行",
        "已展示:",
        "batch_plan",
        "read_artifact",
        file_id,
    ]
    missing = [c for c in checks if c not in block]
    if missing:
        return False, f"missing: {missing}\n--- block ---\n{block[:500]}"
    return True, f"block has all directives, total chars={len(block)}"


# ---------------------------------------------------------------------------
# Test C — batch planner keeps all rows
# ---------------------------------------------------------------------------


def test_c_planner(file_id: str, user_id: str) -> Tuple[bool, str, str]:
    """Returns (passed, message, plan_id)."""
    from core.db.engine import SessionLocal
    from api.routes.v1.internal_batch import _resolve_xlsx_items
    from core.db.models import BatchPlan
    from datetime import datetime, timedelta

    with SessionLocal() as db:
        items, columns, warnings = _resolve_xlsx_items(file_id, db)

        if len(items) != 1114:
            return False, f"items count = {len(items)} != 1114", ""
        if "row" in items[0] and items[0]["row"] != 1:
            return False, "row index in first item is not 1", ""
        if "row" not in items[0]:
            return False, "missing 'row' key in items", ""

        # Persist a confirmed plan we can hand to the orchestrator.
        plan_id = f"bp_test_{uuid.uuid4().hex[:8]}"
        plan = BatchPlan(
            plan_id=plan_id,
            user_id=user_id,
            chat_id=None,
            source_type="xlsx",
            items=items,
            placeholder_keys=["row"] + columns,
            instruction="测试：对每行执行任务",
            prompt_template="[行 {row}] 标题={标题}，类型={类型}",
            max_retries=0,
            status="confirmed",  # skip /confirm endpoint
            progress={"done": 0, "success": 0, "failed": 0},
            expires_at=datetime.utcnow() + timedelta(hours=24),
        )
        db.add(plan)
        db.commit()

    msg = (
        f"items={len(items)} columns={len(columns)} warnings={warnings} "
        f"plan_id={plan_id}"
    )
    return True, msg, plan_id


# ---------------------------------------------------------------------------
# Test D — orchestrator runs through every row (mocked per-item executor)
# ---------------------------------------------------------------------------


async def test_d_orchestrator(plan_id: str, user_id: str) -> Tuple[bool, str]:
    """Replace _run_item_via_workflow with a no-op mock, then run the
    orchestrator. Verifies the iteration loop fires for all 1114 items."""
    import orchestration.batch_orchestrator as orc

    call_count = {"n": 0}

    async def mock_run_item(prompt, user_id, sub_mcp_ids):
        call_count["n"] += 1
        # Return a tiny fake result to keep the orchestrator happy.
        return (f"OK row processed (call #{call_count['n']})", [], [], [])

    original = orc._run_item_via_workflow
    orc._run_item_via_workflow = mock_run_item
    try:
        await orc._run_until_done(plan_id, user_id=user_id)
    finally:
        orc._run_item_via_workflow = original

    # Verify final plan state. Results live inside plan.progress["results"];
    # plan.results is not a column.
    from core.db.engine import SessionLocal
    from core.db.models import BatchPlan

    with SessionLocal() as db:
        plan = db.query(BatchPlan).filter(BatchPlan.plan_id == plan_id).first()
        progress = dict(plan.progress or {})
        results = list(progress.get("results") or [])
        items_count = len(plan.items or [])
        status = plan.status

    if call_count["n"] != 1114:
        return False, f"per-item executor called {call_count['n']} times, expected 1114"
    if items_count != 1114:
        return False, f"plan.items={items_count}, expected 1114"
    if len(results) != 1114:
        return False, (
            f"progress.results has {len(results)} entries, expected 1114; "
            f"counts done={progress.get('done')} success={progress.get('success')} "
            f"failed={progress.get('failed')} status={status}"
        )
    if status != "done":
        return False, f"plan.status={status}, expected 'done'"
    success_count = int(progress.get("success", 0))
    failed_count = int(progress.get("failed", 0))
    return True, (
        f"all 1114 rows processed; status={status} "
        f"success={success_count} failed={failed_count}"
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


async def test_e_read_budget(file_id: str) -> Tuple[bool, str]:
    """Verify read_artifact stops paginating once cumulative reads hit 50K
    on the real file (proves context-explosion guard works in practice)."""
    import json as _json
    from agentscope.tool import Toolkit
    from core.llm.tools.read_artifact_tool import register_read_artifact
    from core.llm.hooks import (
        reset_artifact_read_state,
        MAX_FILE_CONTENT_CHARS,
    )

    toolkit = Toolkit()
    register_read_artifact(toolkit, user_id=None)
    read_artifact = toolkit.tools["read_artifact"].original_func

    reset_artifact_read_state()

    # Simulate a model trying to traverse the whole file via 20K-chunk pages.
    cumulative = 0
    total_chars = None
    refused_at = None
    refusal_msg = ""
    for i in range(20):  # plenty to exhaust 50K budget
        r = await read_artifact(file_id, offset=cumulative, limit=20000)
        block = r.content[0]
        body = _json.loads(block["text"] if isinstance(block, dict) else block.text)
        if "error" in body:
            refused_at = i + 1
            refusal_msg = body["error"]
            break
        cumulative += body["returned_chars"]
        total_chars = body["total_chars"]
        if not body["has_more"] or body["budget_remaining"] == 0:
            # Either consumed everything or budget exactly drained
            continue

    # The real xlsx markdown is huge — we should have hit the cap, not
    # exhausted the file. After cap, cumulative should equal _MAX (50K).
    if cumulative != MAX_FILE_CONTENT_CHARS:
        return False, (
            f"cumulative={cumulative} expected={MAX_FILE_CONTENT_CHARS} "
            f"(refused_at={refused_at}, total_chars={total_chars})"
        )
    if refused_at is None:
        return False, "no refusal triggered — guard didn't engage"
    if "batch_plan" not in refusal_msg:
        return False, f"refusal lacks batch_plan hint: {refusal_msg[:200]}"
    return True, (
        f"capped at {cumulative} chars after {refused_at - 1} successful pages; "
        f"refused on page {refused_at} with batch_plan directive "
        f"(file total_chars={total_chars})"
    )


async def amain(file_path: str) -> int:
    print(f"\n=== End-to-end test: {file_path} ===")
    print(f"file size = {os.path.getsize(file_path)} bytes\n")

    results: List[Tuple[str, bool, str]] = []
    file_id = ""
    plan_id = ""

    # Test A
    print("--- Test A: parse_xlsx_preview ---")
    ok, msg = test_a_preview(file_path)
    print(f"  {'PASS' if ok else 'FAIL'}: {msg}")
    results.append(("A. parse_xlsx_preview", ok, msg))

    # Setup artifact for B+C+D
    print("\n--- Setting up Artifact + storage ---")
    user_id = _resolve_test_user()
    print(f"  using existing user_id = {user_id}")
    file_id = setup_artifact(file_path, user_id)
    print(f"  file_id = {file_id}")

    # Test B
    print("\n--- Test B: file_context xlsx preview block ---")
    ok, msg = test_b_hook_block(file_id)
    print(f"  {'PASS' if ok else 'FAIL'}: {msg}")
    results.append(("B. hook preview block", ok, msg))

    # Test C
    print("\n--- Test C: batch planner keeps all rows ---")
    ok, msg, plan_id = test_c_planner(file_id, user_id)
    print(f"  {'PASS' if ok else 'FAIL'}: {msg}")
    results.append(("C. planner row count", ok, msg))

    # Test D (only if C succeeded)
    if plan_id:
        print("\n--- Test D: orchestrator iterates all rows (mocked LLM) ---")
        ok, msg = await test_d_orchestrator(plan_id, user_id)
        print(f"  {'PASS' if ok else 'FAIL'}: {msg}")
        results.append(("D. orchestrator iteration", ok, msg))

    # Test E
    print("\n--- Test E: read_artifact budget guard on real xlsx ---")
    ok, msg = await test_e_read_budget(file_id)
    print(f"  {'PASS' if ok else 'FAIL'}: {msg}")
    results.append(("E. read_artifact budget", ok, msg))

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    failed = 0
    for name, ok, msg in results:
        flag = "PASS" if ok else "FAIL"
        print(f"  [{flag}] {name}")
        if not ok:
            failed += 1
            print(f"         {msg}")
    print()
    if failed:
        print(f">>> {failed}/{len(results)} TESTS FAILED <<<")
        return 1
    print(f">>> ALL {len(results)} TESTS PASSED <<<")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: e2e_long_xlsx.py <path-to-xlsx>")
        return 2
    path = sys.argv[1]
    if not os.path.exists(path):
        print(f"file not found: {path}")
        return 2
    return asyncio.run(amain(path))


if __name__ == "__main__":
    sys.exit(main())
