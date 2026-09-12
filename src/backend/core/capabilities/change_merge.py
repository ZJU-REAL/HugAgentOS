"""File-level three-way merge; file purpose never changes upload membership."""

import hashlib
import json
import re
from .archive import (
    _normalize_member,
    _validate_names,
    MAX_MEMBERS,
    MAX_TOTAL_BYTES,
    MAX_MEMBER_BYTES,
)
from core.agent_skills.binary_files import decode_binary, is_binary_value


def validate_files(files):
    if not isinstance(files, dict) or len(files) > MAX_MEMBERS:
        raise ValueError("invalid file collection")
    names, size = [], 0
    for name, value in files.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("files must contain encoded strings")
        names.append(_normalize_member(name))
        member_size = len(decode_binary(value) if is_binary_value(value) else value.encode("utf-8"))
        if member_size > MAX_MEMBER_BYTES:
            raise ValueError("file exceeds size limit")
        size += member_size
        if size > MAX_TOTAL_BYTES:
            raise ValueError("file collection exceeds size limit")
    _validate_names(names)
    return files


def revision(files):
    validate_files(files)
    return hashlib.sha256(
        json.dumps(files, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def plan(base, local, cloud):
    validate_files(local)
    validate_files(cloud)
    changes = []
    merged = {}
    for path in sorted(set(base or {}) | set(local) | set(cloud)):
        before, here, there = (base or {}).get(path), local.get(path), cloud.get(path)
        conflict = False
        if here == there:
            selected, side = here, "same"
        elif base is not None and here == before:
            selected, side = there, "cloud"
        elif base is not None and there == before:
            selected, side = here, "local"
        else:
            selected, side, conflict = None, "conflict", True
        if selected is not None:
            merged[path] = selected
        if here != there or (base is not None and before != here):
            changes.append(
                {
                    "path": path,
                    "side": side,
                    "conflict": conflict,
                    "base": before,
                    "local": here,
                    "cloud": there,
                    "binary": any(is_binary_value(v) for v in (here, there) if v is not None),
                }
            )
    return {"changes": changes, "merged": merged}


def resolve(base, local, cloud, choices):
    result = plan(base, local, cloud)
    files = dict(result["merged"])
    known = {row["path"] for row in result["changes"]}
    if set(choices) - known:
        raise ValueError("choice references a file outside the comparison")
    for row in result["changes"]:
        path = row["path"]
        choice = choices.get(path)
        if choice is None:
            if row["conflict"]:
                raise ValueError("unresolved file conflict: " + path)
            continue
        side = choice.get("side")
        if side in ("local", "cloud"):
            value = row[side]
        elif side == "manual" and not row["binary"]:
            value = choice.get("content")
            if not isinstance(value, str):
                raise ValueError("manual resolution needs text")
        elif side == "both":
            alternate = _normalize_member(str(choice.get("alternate_path") or ""))
            if alternate in set(local) | set(cloud) | set(files) | {path}:
                raise ValueError("alternate file already exists")
            if row["local"] is None or row["cloud"] is None:
                raise ValueError("a deleted file cannot be kept twice")
            files[alternate] = row["local"]
            value = row["cloud"]
        else:
            raise ValueError("invalid conflict resolution")
        if value is None:
            files.pop(path, None)
        else:
            files[path] = value
    return validate_files(files)


def sensitive_paths(files):
    pattern = re.compile(
        r"-----BEGIN .*PRIVATE KEY-----|(?:api[_-]?key|token|password|secret|authorization)[\"']?\s*[=:]\s*\S+",
        re.I,
    )
    return [
        path for path, text in files.items() if not is_binary_value(text) and pattern.search(text)
    ]
