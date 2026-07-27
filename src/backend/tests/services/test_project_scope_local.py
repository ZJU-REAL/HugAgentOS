"""Ticket #02 prefactor: CE project-scope carries desktop local-mode fields.

Tests the **CE overlay** ``project_scope`` (the module the desktop CE build runs),
loaded hermetically from ``ce/overlay/…`` so we exercise the exact file that ships
to desktop rather than the EE variant. Skips cleanly when the overlay is absent
(e.g. inside a already-derived CE tree, where ``ce/`` is excluded).

Only ``project_scope_from_context`` is covered here — it is a pure dict→dataclass
transform with no DB access. The default-preserves-cloud-behavior guarantee is the
key assertion: existing personal projects are unchanged.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", "..", ".."))
_OVERLAY = os.path.join(
    _REPO_ROOT, "ce", "overlay", "src", "backend", "core", "services", "project_scope.py"
)

if not os.path.isfile(_OVERLAY):
    pytest.skip("CE overlay project_scope not present (derived tree)", allow_module_level=True)

_spec = importlib.util.spec_from_file_location("ce_project_scope", _OVERLAY)
ps = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ps  # dataclasses needs the module registered
_spec.loader.exec_module(ps)  # type: ignore[union-attr]


def test_local_ctx_yields_local_scope():
    scope = ps.project_scope_from_context(
        {
            "project_id": "p1",
            "project_is_local": True,
            "project_local_slug": "myproj",
            "project_local_path": "/Users/alice/myproj",
        }
    )
    assert scope is not None
    assert scope.is_local is True
    assert scope.kind == "local"
    assert scope.local_path == "/Users/alice/myproj"
    assert scope.local_slug == "myproj"
    assert scope.folder_name == "myproj"
    # a local project is neither a personal-folder nor a team scope
    assert scope.is_personal is False
    assert scope.is_team is False


def test_personal_ctx_unchanged():
    scope = ps.project_scope_from_context(
        {
            "project_id": "p2",
            "project_folder_kind": "personal",
            "project_folder_id": "f9",
            "project_folder_name": "Docs",
        }
    )
    assert scope is not None
    assert scope.is_local is False
    assert scope.kind == "personal"
    assert scope.is_personal is True
    assert scope.local_path is None
    assert scope.local_slug is None
    assert scope.folder_name == "Docs"


def test_no_project_id_returns_none():
    assert ps.project_scope_from_context({}) is None


def test_non_personal_non_local_returns_none():
    # a team-folder ctx (no personal folder, not local) is not a CE scope
    assert ps.project_scope_from_context({"project_id": "p3", "project_folder_kind": "team"}) is None


def test_local_flag_without_paths_still_local():
    scope = ps.project_scope_from_context({"project_id": "p4", "project_is_local": True})
    assert scope is not None and scope.is_local is True
    assert scope.local_path is None and scope.local_slug is None
