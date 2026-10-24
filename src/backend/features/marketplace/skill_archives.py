"""skill archives responsibilities."""
from __future__ import annotations
import io
import os
import zipfile
from pathlib import Path
from typing import Any, Dict, List
from core.capabilities.skills.binary_files import BINARY_EXTENSIONS, JUNK_BASENAMES, MAX_SINGLE_FILE, MAX_TOTAL, decode_binary, encode_binary, is_binary_value
from core.capabilities.skills.deps.detector import detect_dependencies
from core.capabilities.skills.registry import SkillSpecError, load_skill_metadata_from_str, split_frontmatter
from fastapi import HTTPException
from features.marketplace import skills_state



def build_skill_zip(skill_id: str, skill_content: str, extra_files: Dict[str, str]) -> bytes:
    """Pack a DB skill into a zip byte stream (inverse of ``parse_skill_zip``).

    Layout is ``<skill_id>/SKILL.md`` + ``<skill_id>/<relative path>``,
    consistent with the upload/import convention, so an exported package can be
    re-imported directly. base64 values in extra_files carrying the binary
    marker are restored to their original bytes.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{skill_id}/SKILL.md", skill_content or "")
        for rel, stored in (extra_files or {}).items():
            data = decode_binary(stored) if is_binary_value(stored) else str(stored).encode("utf-8")
            zf.writestr(f"{skill_id}/{rel}", data)
    return buf.getvalue()


def build_skill_zip_from_dir(skill_id: str, skill_dir: Path) -> bytes:
    """Pack an on-disk skill directory (built-in/filesystem skills) into a zip byte stream.

    Same layout as ``build_skill_zip``; skips version-control/cache directories
    and junk files, and packs binary files as-is (no base64 round-trip,
    lossless).
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(skill_dir.rglob("*")):
            if not p.is_file() or p.name in JUNK_BASENAMES:
                continue
            rel = p.relative_to(skill_dir)
            if skills_state.ZIP_SKIP_DIR_PARTS.intersection(rel.parts):
                continue
            zf.write(p, f"{skill_id}/{rel.as_posix()}")
    return buf.getvalue()
