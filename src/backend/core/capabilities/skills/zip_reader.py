"""Validate skill upload archives without marketplace persistence."""
from __future__ import annotations
import io
import os
import zipfile
from typing import Any, Dict, List
from core.capabilities.skills.binary_files import BINARY_EXTENSIONS, JUNK_BASENAMES, MAX_SINGLE_FILE, MAX_TOTAL, encode_binary, is_binary_value
from core.capabilities.skills.deps.detector import detect_dependencies
from core.capabilities.skills.registry import SkillSpecError, load_skill_metadata_from_str, split_frontmatter
from fastapi import HTTPException

def parse_skill_zip(data: bytes) -> Dict[str, Any]:
    """Parse a skill zip → returns {skill_id, skill_content (original text), meta, extra_files, dependencies, skipped}.

    Parsing/validation only; nothing is persisted. The same unpacking logic is
    shared by admin market-listing uploads and users' private uploads.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid zip file")

    from core.capabilities.skills.publication import normalize_file_path, safe_skill_id

    # Preserve ZipInfo for reads: normalized names may not exist in the archive.
    entries = {}
    for info in zf.infolist():
        raw_name = info.filename.replace("\\", "/")
        is_dir = raw_name.endswith("/")
        try:
            name = normalize_file_path(raw_name[:-1] if is_dir else raw_name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if is_dir:
            continue
        if name in entries:
            raise HTTPException(status_code=400, detail=f"Duplicate skill file path: {name}")
        entries[name] = info
    # A file cannot also be a parent directory of another file.
    for name in entries:
        parts = name.split("/")
        if any("/".join(parts[:i]) in entries for i in range(1, len(parts))):
            raise HTTPException(status_code=400, detail=f"Conflicting skill file path: {name}")

    skill_md_paths = [n for n in entries if n.split("/")[-1] == "SKILL.md"]
    if not skill_md_paths:
        raise HTTPException(status_code=400, detail="No SKILL.md found in zip")

    skill_md_path = skill_md_paths[0]
    parts = skill_md_path.split("/")
    if len(parts) == 1:
        prefix = ""
    elif len(parts) == 2:
        prefix = parts[0] + "/"
    else:
        prefix = "/".join(parts[:-1]) + "/"

    try:
        raw = zf.read(entries[skill_md_path]).decode("utf-8")
        fm, _ = split_frontmatter(raw)
        skill_id = fm.get("name", "").strip()
        if not skill_id:
            if prefix:
                skill_id = prefix.rstrip("/").split("/")[-1]
            else:
                raise HTTPException(status_code=400, detail="SKILL.md missing 'name' in frontmatter")
    except SkillSpecError as e:
        raise HTTPException(status_code=400, detail=f"Invalid SKILL.md: {e}")

    try:
        safe_skill_id(skill_id)
        meta = load_skill_metadata_from_str(raw, skill_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Invalid skill: {e}")

    JUNK_PATH_MARKERS = ("__MACOSX/", "/.git/", "/__pycache__/", "/.svn/", "/.hg/")
    extra_files: Dict[str, str] = {}
    skipped: List[Dict[str, str]] = []
    total_size = 0
    for entry, info in entries.items():
        if entry == skill_md_path:
            continue
        if prefix and not entry.startswith(prefix):
            continue
        rel_name = entry[len(prefix):] if prefix else entry
        if not rel_name:
            continue
        base = rel_name.rsplit("/", 1)[-1]
        if base in JUNK_BASENAMES or any(m in f"/{entry}" for m in JUNK_PATH_MARKERS):
            continue
        if info.file_size > MAX_SINGLE_FILE:
            skipped.append({"file": rel_name, "reason": f"exceeds {MAX_SINGLE_FILE // (1024 * 1024)}MB single-file ceiling"})
            continue
        try:
            file_bytes = zf.read(info)
        except (KeyError, zipfile.BadZipFile):
            skipped.append({"file": rel_name, "reason": "unreadable zip entry"})
            continue
        _, ext = os.path.splitext(rel_name)
        if ext.lower() in BINARY_EXTENSIONS:
            stored = encode_binary(file_bytes)
        else:
            try:
                stored = file_bytes.decode("utf-8")
            except UnicodeDecodeError:
                stored = encode_binary(file_bytes)
        stored_size = len(stored.encode("utf-8"))
        if total_size + stored_size > MAX_TOTAL:
            skipped.append({"file": rel_name, "reason": f"would exceed {MAX_TOTAL // (1024 * 1024)}MB total ceiling"})
            continue
        total_size += stored_size
        extra_files[rel_name] = stored

    dependencies = detect_dependencies(
        {fn: c for fn, c in extra_files.items() if not is_binary_value(c)}
    )
    return {
        "skill_id": skill_id,
        "skill_content": raw,
        "meta": meta,
        "extra_files": extra_files,
        "dependencies": dependencies,
        "skipped": skipped,
    }
