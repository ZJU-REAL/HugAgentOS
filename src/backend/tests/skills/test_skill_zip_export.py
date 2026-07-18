"""Round-trip tests for skill zip export (build_skill_zip / build_skill_zip_from_dir) and import."""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi import HTTPException

from core.agent_skills.binary_files import decode_binary, encode_binary, encode_upload, is_binary_value
from core.services.marketplace_service import (
    build_skill_zip,
    build_skill_zip_from_dir,
    parse_skill_zip,
)

SKILL_MD = """---
name: demo-skill
display_name: 演示技能
description: 用于测试导出的演示技能
version: 1.2.0
---

## 步骤

1. 做点什么
"""

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_build_skill_zip_roundtrip():
    extra = {
        "scripts/run.py": "print('hi')\n",
        "assets/logo.png": encode_binary(PNG_BYTES),
    }
    data = build_skill_zip("demo-skill", SKILL_MD, extra)

    zf = zipfile.ZipFile(io.BytesIO(data))
    names = set(zf.namelist())
    assert names == {
        "demo-skill/SKILL.md",
        "demo-skill/scripts/run.py",
        "demo-skill/assets/logo.png",
    }
    # binary-marked value is restored to the original bytes (not base64 text)
    assert zf.read("demo-skill/assets/logo.png") == PNG_BYTES

    # the exported package can be parsed back as-is by parse_skill_zip
    parsed = parse_skill_zip(data)
    assert parsed["skill_id"] == "demo-skill"
    assert parsed["skill_content"] == SKILL_MD
    assert parsed["extra_files"]["scripts/run.py"] == "print('hi')\n"
    stored_png = parsed["extra_files"]["assets/logo.png"]
    assert is_binary_value(stored_png)
    assert decode_binary(stored_png) == PNG_BYTES


def test_build_skill_zip_from_dir(tmp_path):
    root = tmp_path / "demo-skill"
    (root / "scripts").mkdir(parents=True)
    (root / "__pycache__").mkdir()
    (root / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    (root / "scripts" / "run.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "assets").mkdir()
    (root / "assets" / "logo.png").write_bytes(PNG_BYTES)
    (root / "__pycache__" / "run.cpython-311.pyc").write_bytes(b"\x00")
    (root / ".DS_Store").write_bytes(b"\x00")

    data = build_skill_zip_from_dir("demo-skill", root)
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = set(zf.namelist())
    assert names == {
        "demo-skill/SKILL.md",
        "demo-skill/scripts/run.py",
        "demo-skill/assets/logo.png",
    }
    assert zf.read("demo-skill/assets/logo.png") == PNG_BYTES
    assert parse_skill_zip(data)["skill_id"] == "demo-skill"


def test_encode_upload_text_and_binary():
    assert encode_upload("a.py", b"print(1)\n") == "print(1)\n"
    stored = encode_upload("logo.png", PNG_BYTES)
    assert is_binary_value(stored)
    assert decode_binary(stored) == PNG_BYTES
    # no extension but not decodable as UTF-8 -> fall back to binary
    stored2 = encode_upload("blob", b"\xff\xfe\x00\x01")
    assert is_binary_value(stored2)


def test_validate_skill_file_path():
    from api.routes.v1.admin_skills import _validate_skill_file_path

    assert _validate_skill_file_path("scripts/run.py") == "scripts/run.py"
    assert _validate_skill_file_path("/config.json") == "config.json"
    for bad in ("../evil.py", "a/../../b", "a\\b", "", "a//b", "."):
        with pytest.raises(HTTPException):
            _validate_skill_file_path(bad)
