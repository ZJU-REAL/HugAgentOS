"""Tests for the extra_files feature across all layers.

Run with:  cd src/backend && python -m pytest tests/test_extra_files.py -v
"""

from __future__ import annotations

import io
import os
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── 1. Registry: AgentSkillSpec has extra_files / base_dir fields ─────────

def test_agent_skill_spec_has_extra_files():
    from core.agent_skills.registry import AgentSkillSpec

    spec = AgentSkillSpec(
        id="test", name="test", description="test desc", version="1.0.0",
        instructions=["do this"],
        extra_files=["a.md", "b.json"],
        base_dir="/tmp/test",
    )
    assert spec.extra_files == ["a.md", "b.json"]
    assert spec.base_dir == "/tmp/test"


def test_agent_skill_spec_extra_files_defaults_empty():
    from core.agent_skills.registry import AgentSkillSpec

    spec = AgentSkillSpec(
        id="test", name="test", description="test desc", version="1.0.0",
        instructions=["do this"],
    )
    assert spec.extra_files == []
    assert spec.base_dir == ""


# ── 2. FilesystemBackend.get_extra_files ──────────────────────────────────

def test_filesystem_backend_get_extra_files():
    from core.agent_skills.backends.filesystem import FilesystemBackend

    with tempfile.TemporaryDirectory() as tmpdir:
        skill_dir = Path(tmpdir) / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: my-skill\n---\n")
        (skill_dir / "config.json").write_text('{"key": "value"}')
        (skill_dir / "templates").mkdir()
        (skill_dir / "templates" / "report.md").write_text("# Report")
        (skill_dir / "binary.png").write_bytes(b"\x89PNG")

        backend = FilesystemBackend(root_dir=tmpdir, source_name="test")
        files = backend.get_extra_files("my-skill")

        assert "config.json" in files
        assert files["config.json"] == '{"key": "value"}'
        assert "templates/report.md" in files
        assert files["templates/report.md"] == "# Report"
        assert "SKILL.md" not in files
        assert "binary.png" not in files


def test_filesystem_backend_get_extra_files_missing_skill():
    from core.agent_skills.backends.filesystem import FilesystemBackend

    with tempfile.TemporaryDirectory() as tmpdir:
        backend = FilesystemBackend(root_dir=tmpdir, source_name="test")
        assert backend.get_extra_files("nonexistent") == {}


# ── 3. DatabaseBackend.get_extra_files ────────────────────────────────────

def test_database_backend_get_extra_files():
    from core.agent_skills.backends.database import DatabaseBackend

    mock_row = MagicMock()
    mock_row.extra_files = {"data.csv": "a,b,c\n1,2,3"}
    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.first.return_value = mock_row

    backend = DatabaseBackend()
    with patch.object(backend, "_get_session_and_model") as mock_gsm:
        mock_gsm.return_value = (MagicMock(return_value=mock_session), MagicMock())
        files = backend.get_extra_files("test-skill")

    assert files == {"data.csv": "a,b,c\n1,2,3"}


def test_database_backend_get_extra_files_none():
    from core.agent_skills.backends.database import DatabaseBackend

    mock_row = MagicMock()
    mock_row.extra_files = None
    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.first.return_value = mock_row

    backend = DatabaseBackend()
    with patch.object(backend, "_get_session_and_model") as mock_gsm:
        mock_gsm.return_value = (MagicMock(return_value=mock_session), MagicMock())
        assert backend.get_extra_files("test-skill") == {}


def test_database_backend_get_extra_files_skill_not_found():
    from core.agent_skills.backends.database import DatabaseBackend

    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.first.return_value = None

    backend = DatabaseBackend()
    with patch.object(backend, "_get_session_and_model") as mock_gsm:
        mock_gsm.return_value = (MagicMock(return_value=mock_session), MagicMock())
        assert backend.get_extra_files("missing") == {}


# ── 4. CompositeBackend.get_extra_files ───────────────────────────────────

def test_composite_backend_get_extra_files():
    from core.agent_skills.backends.composite import CompositeBackend
    from core.agent_skills.backends.protocol import SkillFileInfo

    mock_fs = MagicMock()
    mock_fs.source_name = "built-in"
    mock_fs.priority = 10
    mock_fs.list_skill_files.return_value = [
        SkillFileInfo(skill_id="my-skill", file_path=Path("/x/SKILL.md"),
                      source_name="built-in", priority=10)
    ]
    mock_fs.get_extra_files.return_value = {"readme.txt": "hello"}

    mock_db = MagicMock()
    mock_db.source_name = "admin"
    mock_db.priority = 75
    mock_db.list_skill_files.return_value = []

    composite = CompositeBackend([mock_fs, mock_db])
    assert composite.get_extra_files("my-skill") == {"readme.txt": "hello"}


def test_composite_backend_get_extra_files_not_found():
    from core.agent_skills.backends.composite import CompositeBackend

    mock = MagicMock()
    mock.source_name = "test"
    mock.priority = 10
    mock.list_skill_files.return_value = []

    composite = CompositeBackend([mock])
    assert composite.get_extra_files("nonexistent") == {}


# ── 5. Zip extraction logic ──────────────────────────────────────────────

def _make_zip(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


SKILL_MD = """---
name: test-skill
display_name: Test Skill
description: A test skill
version: 1.0.0
---

## Instructions

1. Do something
"""


def test_zip_extraction_basic():
    TEXT_EXTENSIONS = {
        ".md", ".txt", ".json", ".py", ".yaml", ".yml", ".toml", ".cfg",
        ".ini", ".csv", ".xml", ".html", ".css", ".js", ".ts", ".sh", ".conf",
    }
    zip_data = _make_zip({
        "test-skill/SKILL.md": SKILL_MD,
        "test-skill/config.json": '{"key": "value"}',
        "test-skill/templates/report.md": "# Report",
        "test-skill/data.csv": "a,b,c\n1,2,3",
        "test-skill/image.png": b"\x89PNG\r\n\x1a\n",
        "test-skill/script.py": "print('hello')",
    })
    zf = zipfile.ZipFile(io.BytesIO(zip_data))
    prefix = "test-skill/"

    extra_files = {}
    for entry in zf.namelist():
        if entry == "test-skill/SKILL.md" or entry.endswith("/"):
            continue
        if not entry.startswith(prefix):
            continue
        _, ext = os.path.splitext(entry)
        if ext.lower() not in TEXT_EXTENSIONS:
            continue
        try:
            content = zf.read(entry).decode("utf-8")
        except (UnicodeDecodeError, KeyError):
            continue
        extra_files[entry[len(prefix):]] = content

    assert set(extra_files.keys()) == {"config.json", "templates/report.md", "data.csv", "script.py"}
    assert "image.png" not in extra_files


def test_zip_extraction_flat():
    zip_data = _make_zip({"SKILL.md": SKILL_MD, "config.json": '{"flat": true}', "readme.txt": "hi"})
    zf = zipfile.ZipFile(io.BytesIO(zip_data))
    TEXT_EXTENSIONS = {".json", ".txt"}

    extra_files = {}
    for entry in zf.namelist():
        if entry == "SKILL.md" or entry.endswith("/"):
            continue
        _, ext = os.path.splitext(entry)
        if ext.lower() not in TEXT_EXTENSIONS:
            continue
        extra_files[entry] = zf.read(entry).decode("utf-8")

    assert "config.json" in extra_files
    assert "readme.txt" in extra_files


# ── 6. DB model has extra_files column ────────────────────────────────────

def test_admin_skill_model_has_extra_files():
    from core.db.models import AdminSkill
    assert hasattr(AdminSkill, "extra_files")
    col = AdminSkill.__table__.columns["extra_files"]
    # The model uses JSONType = JSON().with_variant(JSONB(), "postgresql"): the class
    # name is the JSON base class, but under PostgreSQL it is actually JSONB. Verify the
    # JSON family + postgres variant is JSONB.
    type_name = col.type.__class__.__name__
    assert type_name in ("JSON", "JSONB"), f"expected JSON-family, got {type_name}"
    if type_name == "JSON":
        from sqlalchemy.dialects.postgresql import JSONB
        variant = getattr(col.type, "_variant_mapping", {}) or {}
        pg = variant.get("postgresql")
        assert pg is not None and isinstance(pg, JSONB), "extra_files 应有 postgresql JSONB variant"


# ── 7. Migration file is valid ────────────────────────────────────────────

def test_migration_file_valid():
    migration_path = Path(__file__).resolve().parent.parent.parent / "alembic" / "versions" / "b2c3d4e5f6g7_add_extra_files_to_admin_skills.py"
    assert migration_path.exists()
    content = migration_path.read_text()
    assert "extra_files" in content
    assert "JSONB" in content


# ── 8. Loader get_extra_files delegates to backend ────────────────────────

def test_loader_get_extra_files():
    from core.agent_skills.loader import MultiSourceSkillLoader

    mock_backend = MagicMock()
    mock_backend.get_extra_files.return_value = {"f.txt": "content"}

    loader = MultiSourceSkillLoader(backend=mock_backend)
    assert loader.get_extra_files("some-skill") == {"f.txt": "content"}


# ── 9. Loader.get_skill_dir for filesystem skills ────────────────────────

def test_loader_get_skill_dir_filesystem():
    from core.agent_skills.loader import MultiSourceSkillLoader
    from core.agent_skills.backends.protocol import SkillFileInfo

    mock_backend = MagicMock()
    mock_backend.get_skill_info.return_value = SkillFileInfo(
        skill_id="test", file_path=Path("/opt/skills/test/SKILL.md"),
        source_name="built-in", priority=10, content=None,
    )
    loader = MultiSourceSkillLoader(backend=mock_backend)
    assert loader.get_skill_dir("test") == "/opt/skills/test"


# ── 10. Loader.get_skill_dir for DB skills (materialization) ─────────────

def test_loader_get_skill_dir_db_materializes():
    from core.agent_skills.loader import MultiSourceSkillLoader
    from core.agent_skills.backends.protocol import SkillFileInfo

    mock_backend = MagicMock()
    mock_backend.get_skill_info.return_value = SkillFileInfo(
        skill_id="db-skill", file_path=Path("/db/admin_skills/db-skill/SKILL.md"),
        source_name="admin", priority=75,
        content="---\nname: db-skill\ndescription: test\n---\n## Instructions\n1. do\n",
    )
    mock_backend.get_extra_files.return_value = {"config.json": '{"x":1}'}

    loader = MultiSourceSkillLoader(backend=mock_backend)
    skill_dir = loader.get_skill_dir("db-skill")

    assert skill_dir is not None
    p = Path(skill_dir)
    assert p.is_dir()
    assert (p / "SKILL.md").is_file()
    assert (p / "config.json").is_file()
    assert (p / "config.json").read_text() == '{"x":1}'

    # Cleanup
    import shutil
    shutil.rmtree(p)


# ── 11. register_skills_to_toolkit with AgentScope Toolkit ───────────────

def test_register_skills_to_toolkit():
    from agentscope.tool import Toolkit
    from core.llm.tool_collector import ToolCollector
    from core.agent_skills.loader import MultiSourceSkillLoader

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a real skill directory
        skill_dir = Path(tmpdir) / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: test-skill\ndescription: A test\n---\n## Instructions\n1. do\n"
        )
        (skill_dir / "config.json").write_text('{"key": "val"}')

        from core.agent_skills.backends.filesystem import FilesystemBackend
        from core.agent_skills.backends.composite import CompositeBackend

        backend = CompositeBackend([FilesystemBackend(root_dir=tmpdir, source_name="test")])
        loader = MultiSourceSkillLoader(backend=backend)

        # AgentScope 2.0: register_skills_to_toolkit writes into ToolCollector (register_agent_skill);
        # the collected skill directories live in collector.skill_loaders, passed to Toolkit(skills_or_loaders=).
        c = ToolCollector()
        n = loader.register_skills_to_toolkit(c)
        assert n == 1
        assert len(c.skill_loaders) == 1
        # A real Toolkit can be constructed from the collected skill directories (2.0 skill prompts go through the internal skill_instruction_template)
        tk = Toolkit(skills_or_loaders=c.skill_loaders)
        assert tk is not None
        # 2.0: the skill directory is collected by the collector (skill_loaders contains that dir)
        assert any(str(skill_dir) in str(s) for s in c.skill_loaders)
