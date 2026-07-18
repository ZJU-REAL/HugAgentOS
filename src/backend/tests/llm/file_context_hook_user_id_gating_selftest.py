"""Selftest: the file_context hook validates ownership by user_id when pulling attachment content.

Background
----------
`core/llm/hooks.py::_build_file_context` assembles the parsed text / xlsx
preview / image base64 of ``uploaded_files`` (i.e. the ``attachments`` in the
request body) into context injected into agent memory, as input to ReAct
reasoning.

The old implementation had three "bare read" paths that were blind to
``ctx.user_id``:

1. ``_build_file_context`` → ``fetch_parsed_text(fid)`` — user_id was not
   passed, so the ownership check inside
   :func:`core.content.artifact_reader.fetch_parsed_text`
   ``if user_id and art.user_id != user_id:`` was **skipped entirely**
   because user_id was falsy.
2. ``_build_xlsx_preview_block`` → ``_download_artifact_bytes(...)`` —
   likewise no user_id; after resolving the storage_key it pulled raw bytes
   directly into ``parse_xlsx_preview``.
3. ``_fetch_image_base64`` → ``_download_artifact_bytes(...)`` — same as above.

Meanwhile the ``uploaded_files`` field in
``api/routes/v1/chats.py::_build_ctx`` is a **direct copy** of
``request.attachments`` (client-supplied); the HTTP boundary does **not**
verify per item whether ``file_id`` belongs to the current user. Combined
with the three bare-read paths above, an attacker only needs to stuff
someone else's ``ua_xxx`` they have seen into ``attachments[].file_id`` to
get that other user's parsed text / full-page xlsx preview / image base64
**into their own agent's memory**, after which the LLM answer regurgitates
that content verbatim — a classic cross-user data-leak vector.

Reference for comparison: ``core/llm/tools/read_tool.py:271`` uses
``fetch_parsed_text(fid, user_id)`` — the ReAct tool side already does it
correctly; the hook side slipped through the net.

Fix
---
Thread ``user_id`` all the way through the three bare-read paths:

- ``_build_file_context(uploaded_files, user_id=...)``
  → ``fetch_parsed_text(fid, user_id=user_id)``
  → ``_build_xlsx_preview_block(f, user_id=user_id)``
- ``_build_xlsx_preview_block(f, user_id=...)``
  → ``_download_artifact_bytes(..., user_id=user_id)``
- ``_fetch_image_base64(f, user_id=...)``
  → ``_download_artifact_bytes(..., user_id=user_id)``
- ``_download_artifact_bytes(..., user_id=...)`` verifies ownership via
  ``load_artifact_meta(file_id, user_id=user_id)`` before hitting storage;
  on verification failure it immediately ``return None`` (aligned with the
  semantics of ``fetch_parsed_text``: wrong ownership is treated as
  not-read, no exception raised).
- ``make_file_context_hook::file_context_pre_reply`` extracts ``user_id``
  from ``agent._jx_context`` and passes it to the helpers above.
- The non-streaming path's ``_build_file_context`` call in
  ``routing/workflow.py`` also gains ``user_id=(ctx.user_id or None)``.

``user_id=None`` still takes the old "no ownership check" branch, preserving
the call semantics of the downstream
``core/llm/tool.py::_probe_xlsx_sheet_names`` /
``_probe_pptx_slide_count`` sheet / slide count probes — in those two
contexts, ``fetch_parsed_text(file_id, user_id=user_id)`` follows
immediately after and is the authoritative gate; this PR does not touch
their contract.

This test has zero third-party dependencies — it parses the ``hooks.py``
source directly with ``ast`` plus injects a fake implementation of
``_download_artifact_bytes`` for a behavior-level replay, verifying:

1. All three helper signatures gained a ``user_id`` parameter (keyword,
   optional, default None).
2. ``_download_artifact_bytes`` actually calls
   ``load_artifact_meta(file_id, user_id=user_id)`` and does ``return None``
   when meta is None (no storage access).
3. ``_build_file_context`` passes ``user_id`` through when calling
   ``fetch_parsed_text``.
4. ``_build_file_context`` passes ``user_id`` through when calling
   ``_build_xlsx_preview_block``.
5. ``_build_xlsx_preview_block`` / ``_fetch_image_base64`` pass ``user_id``
   through when calling ``_download_artifact_bytes``.
6. ``make_file_context_hook::file_context_pre_reply`` passes ``ctx.user_id``
   to ``_build_file_context`` / ``_fetch_image_base64``.
7. The non-streaming path's ``_build_file_context`` call in
   ``routing/workflow.py`` carries a ``user_id=`` keyword argument.

Any failed pin = regression.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_HOOKS_PATH = _REPO_ROOT / "backend" / "core" / "llm" / "hooks.py"
_WORKFLOW_PATH = _REPO_ROOT / "backend" / "routing" / "workflow.py"


# ── Helpers: extract function definitions from the ast ─────────────────────


def _read_module(path: Path) -> ast.Module:
    src = path.read_text(encoding="utf-8")
    return ast.parse(src, filename=str(path))


def _find_func(mod: ast.Module, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    # Look both at top-level and nested inside enclosing functions (hook-factory style)
    candidates: list[ast.AST] = []
    for node in ast.walk(mod):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            candidates.append(node)
    if not candidates:
        raise AssertionError(f"function {name!r} not found")
    if len(candidates) > 1:
        # Take the deepest one (avoids mistakenly picking an import name shadow)
        return candidates[-1]  # type: ignore[return-value]
    return candidates[0]  # type: ignore[return-value]


def _has_kw_arg(func: ast.AST, name: str) -> bool:
    args = getattr(func, "args", None)
    if args is None:
        return False
    return any(a.arg == name for a in (args.args or [])) or any(
        a.arg == name for a in (args.kwonlyargs or [])
    )


def _call_kwargs(call: ast.Call) -> dict[str, ast.AST]:
    return {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}


def _calls_in(func: ast.AST, callee_name: str) -> list[ast.Call]:
    out: list[ast.Call] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id == callee_name:
                out.append(node)
            elif isinstance(f, ast.Attribute) and f.attr == callee_name:
                out.append(node)
    return out


# ── pins ───────────────────────────────────────────────────────────────


def pin_helper_signatures_take_user_id() -> None:
    mod = _read_module(_HOOKS_PATH)
    for name in ("_download_artifact_bytes", "_fetch_image_base64",
                 "_build_xlsx_preview_block", "_build_file_context"):
        func = _find_func(mod, name)
        assert _has_kw_arg(func, "user_id"), (
            f"{name} 应该带 user_id 参数（关键字、可选、默认 None）"
        )
    print("  ✓ 四个 helper 的签名都新增了 user_id 关键字参数")


def pin_download_bytes_gates_on_load_artifact_meta() -> None:
    mod = _read_module(_HOOKS_PATH)
    func = _find_func(mod, "_download_artifact_bytes")
    calls = _calls_in(func, "load_artifact_meta")
    assert calls, (
        "_download_artifact_bytes 应该调 load_artifact_meta(file_id, user_id=...)"
        " 做归属校验"
    )
    has_correct = False
    for c in calls:
        kw = _call_kwargs(c)
        user_id_arg = kw.get("user_id")
        if isinstance(user_id_arg, ast.Name) and user_id_arg.id == "user_id":
            has_correct = True
    assert has_correct, (
        "load_artifact_meta 调用必须把当前 user_id 透传给 meta 校验"
    )
    print("  ✓ _download_artifact_bytes 用 load_artifact_meta(file_id, user_id=user_id) 做归属闸门")


def pin_build_file_context_passes_user_id_to_fetch_parsed_text() -> None:
    mod = _read_module(_HOOKS_PATH)
    func = _find_func(mod, "_build_file_context")
    calls = _calls_in(func, "fetch_parsed_text")
    assert calls, "_build_file_context 里仍需调用 fetch_parsed_text"
    for c in calls:
        kw = _call_kwargs(c)
        arg = kw.get("user_id")
        assert isinstance(arg, ast.Name) and arg.id == "user_id", (
            "fetch_parsed_text 调用应当显式带 user_id=user_id"
        )
    print("  ✓ _build_file_context → fetch_parsed_text(fid, user_id=user_id) 透传归属")


def pin_build_file_context_passes_user_id_to_xlsx_block() -> None:
    mod = _read_module(_HOOKS_PATH)
    func = _find_func(mod, "_build_file_context")
    calls = _calls_in(func, "_build_xlsx_preview_block")
    assert calls, (
        "_build_file_context 仍应分流 xlsx 到 _build_xlsx_preview_block"
    )
    for c in calls:
        kw = _call_kwargs(c)
        arg = kw.get("user_id")
        assert isinstance(arg, ast.Name) and arg.id == "user_id", (
            "_build_xlsx_preview_block 调用应当显式带 user_id=user_id"
        )
    print("  ✓ _build_file_context → _build_xlsx_preview_block(..., user_id=user_id) 透传归属")


def pin_xlsx_and_image_helpers_pass_user_id_to_downloader() -> None:
    mod = _read_module(_HOOKS_PATH)
    for helper in ("_build_xlsx_preview_block", "_fetch_image_base64"):
        func = _find_func(mod, helper)
        calls = _calls_in(func, "_download_artifact_bytes")
        assert calls, f"{helper} 仍应调 _download_artifact_bytes"
        for c in calls:
            kw = _call_kwargs(c)
            arg = kw.get("user_id")
            assert isinstance(arg, ast.Name) and arg.id == "user_id", (
                f"{helper} 调 _download_artifact_bytes 应当带 user_id=user_id"
            )
    print("  ✓ _build_xlsx_preview_block / _fetch_image_base64 → _download_artifact_bytes(..., user_id=user_id)")


def pin_hook_extracts_user_id_from_ctx_and_propagates() -> None:
    mod = _read_module(_HOOKS_PATH)
    pre_reply = _find_func(mod, "file_context_pre_reply")
    # There must be a local assignment named ctx_user_id
    found_assignment = False
    for node in ast.walk(pre_reply):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "ctx_user_id":
                    found_assignment = True
        elif isinstance(node, ast.AnnAssign):
            tgt = node.target
            if isinstance(tgt, ast.Name) and tgt.id == "ctx_user_id":
                found_assignment = True
    assert found_assignment, (
        "file_context_pre_reply 应当从 ctx 上抽出 ctx_user_id 后透传给后续 helper"
    )
    # _build_file_context / _fetch_image_base64 calls must both carry user_id=ctx_user_id
    for helper in ("_build_file_context", "_fetch_image_base64"):
        calls = _calls_in(pre_reply, helper)
        assert calls, f"file_context_pre_reply 仍应调用 {helper}"
        for c in calls:
            kw = _call_kwargs(c)
            arg = kw.get("user_id")
            assert isinstance(arg, ast.Name) and arg.id == "ctx_user_id", (
                f"{helper} 调用应当带 user_id=ctx_user_id（实际：{ast.unparse(arg) if arg else 'missing'}）"
            )
    print("  ✓ file_context_pre_reply 抽 ctx.user_id → 透传给 _build_file_context / _fetch_image_base64")


def pin_workflow_non_streaming_path_passes_user_id() -> None:
    mod = _read_module(_WORKFLOW_PATH)
    # _build_file_context is brought into workflow.py via from-import, so it
    # suffices to find calls to it inside module-top-level nested functions
    # (it is an async fn nested inside _run)
    calls: list[ast.Call] = []
    for node in ast.walk(mod):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id == "_build_file_context":
                calls.append(node)
    assert calls, "routing/workflow.py 非流式路径仍应调用 _build_file_context"
    for c in calls:
        kw = _call_kwargs(c)
        assert "user_id" in kw, (
            "workflow.py 非流式路径调 _build_file_context 应当显式带 user_id"
        )
    print("  ✓ routing/workflow.py 非流式路径 _build_file_context(..., user_id=...) 已加")


# ── Behavior-level replay: mock the key dependencies of the hooks module, then actually run _download_artifact_bytes once ──


def behavior_download_returns_none_when_user_id_mismatch() -> None:
    """Key behavior: when user_id does not match, _download_artifact_bytes
    returns None directly, and storage.download_bytes must **not be called**.

    This test does **not depend on** httpx / pydantic / agentscope — the
    hooks module's import chain is too heavy, and running with it easily
    yields false failures in CI environments due to missing third-party
    packages. Instead, extract the function source via AST + exec it in an
    isolated namespace, replaying with pure stub dependencies — same
    approach as the selftest in PR #16.
    """
    import types as _types

    src = _HOOKS_PATH.read_text(encoding="utf-8")
    mod = ast.parse(src)
    func_node: ast.AST | None = None
    for node in ast.walk(mod):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_download_artifact_bytes":
            func_node = node
            break
    assert func_node is not None, "_download_artifact_bytes 找不到"

    func_src = ast.unparse(func_node)

    # ── Prepare stubs for the isolated namespace ─────────────────────────
    storage_calls = {"n": 0}

    class _FakeStorage:
        def download_bytes(self, key: str) -> bytes:  # noqa: D401
            storage_calls["n"] += 1
            return b"stolen-bytes"

    class _StorageError(Exception):
        pass

    artifact_reader_stub = _types.SimpleNamespace(
        load_artifact_meta=lambda file_id, user_id=None: None,
        resolve_artifact_storage=lambda file_id, fallback_name="file": (
            "storage_key_fake", fallback_name,
        ),
    )
    storage_stub = _types.SimpleNamespace(get_storage=lambda: _FakeStorage())
    infra_exc = _types.SimpleNamespace(StorageError=_StorageError)

    fake_modules = {
        "core.content.artifact_reader": artifact_reader_stub,
        "core.storage": storage_stub,
        "core.infra.exceptions": infra_exc,
    }

    # Intercept the ImportError of `from core.X import Y` in the namespace and
    # let stubs inject the target symbols into the local namespace. The
    # cleanest approach: stub `sys.modules` directly, then exec. This path has
    # a transient side effect, cleaned up in a finally block afterwards.
    import sys as _sys
    saved = {k: _sys.modules.get(k) for k in fake_modules}
    try:
        for k, v in fake_modules.items():
            _sys.modules[k] = v  # type: ignore[assignment]

        ns: dict[str, object] = {
            "Optional": __import__("typing").Optional,
            "logger": _types.SimpleNamespace(warning=lambda *a, **kw: None),
        }
        exec(func_src, ns)
        _download = ns["_download_artifact_bytes"]

        # Mismatch scenario: load_artifact_meta returns None → no storage access
        storage_calls["n"] = 0
        out = _download(
            "ua_someone_else", "x.docx", "selftest", user_id="me_attacker",
        )
        assert out is None, "归属校验未通过时必须 return None"
        assert storage_calls["n"] == 0, (
            f"归属校验未通过时绝不能下 storage（实际下了 {storage_calls['n']} 次）"
        )
        print("  ✓ user_id 不匹配 → 不下 storage、return None")

        # OK scenario: load_artifact_meta returns non-None (owned) → hit storage once
        artifact_reader_stub.load_artifact_meta = (  # type: ignore[attr-defined]
            lambda file_id, user_id=None: {"file_id": file_id}
        )
        storage_calls["n"] = 0
        out = _download(
            "ua_mine", "x.docx", "selftest", user_id="me_owner",
        )
        assert out == b"stolen-bytes", "归属通过时应当真的返回 storage bytes"
        assert storage_calls["n"] == 1
        print("  ✓ user_id 匹配 → 正常下 storage 一次")

        # Legacy-call compatibility: with user_id=None no ownership check is done,
        # storage is still accessed (preserves the probe fallback semantics)
        artifact_reader_stub.load_artifact_meta = (  # type: ignore[attr-defined]
            lambda file_id, user_id=None: None  # even if meta rejects, user_id=None takes the old path
        )
        storage_calls["n"] = 0
        out = _download(
            "ua_legacy", "x.docx", "selftest",  # user_id not passed
        )
        assert out == b"stolen-bytes", "user_id=None 时按旧契约下 storage"
        assert storage_calls["n"] == 1
        print("  ✓ user_id=None → 沿用旧契约（保留 probe 调用点的兼容性）")
    finally:
        # Remove the stubs
        for k, prev in saved.items():
            if prev is None:
                _sys.modules.pop(k, None)
            else:
                _sys.modules[k] = prev


def main() -> int:
    print("=== file_context_hook_user_id_gating_selftest ===")
    pin_helper_signatures_take_user_id()
    pin_download_bytes_gates_on_load_artifact_meta()
    pin_build_file_context_passes_user_id_to_fetch_parsed_text()
    pin_build_file_context_passes_user_id_to_xlsx_block()
    pin_xlsx_and_image_helpers_pass_user_id_to_downloader()
    pin_hook_extracts_user_id_from_ctx_and_propagates()
    pin_workflow_non_streaming_path_passes_user_id()
    behavior_download_returns_none_when_user_id_mismatch()
    print("=== file_context_hook_user_id_gating_selftest: OK ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
