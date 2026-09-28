"""Frontend skill editing uses the same immutable installation lifecycle as tools."""
import tempfile
from pathlib import Path
from core.capabilities import archive, registry, store
from core.services import local_skill_service as lifecycle
from core.agent_skills.binary_files import encode_upload, is_binary_value
from core.agent_skills.registry import _split_frontmatter
from core.services.skill_management_service import build_skill_content, validate_skill_file_path

def frontend_errors(function):
    from functools import wraps
    @wraps(function)
    def call(*args, **kwargs):
        from fastapi import HTTPException
        from core.capabilities.errors import CapabilityError
        try:
            return function(*args, **kwargs)
        except PermissionError as exc:
            raise HTTPException(404, detail="Installation not found") from exc
        except (ValueError, CapabilityError) as exc:
            raise HTTPException(409 if "conflict" in str(exc) else 422, detail=str(exc)) from exc
    return call


def _list(value):
    return value if isinstance(value, list) else str(value or "").replace(",", " ").split()


def read(user_id, key):
    inst = lifecycle._owned(user_id, registry.install_id("skill", "local", key))
    comp = store.get("skill", "local", inst.key, inst.resolved_revision)
    if comp is None:
        raise ValueError("installed skill files missing")
    files = {name: p.read_bytes() for name, p in archive.iter_files(comp.path) if name != ".inventory.json"}
    return inst, files


@frontend_errors
def detail(user_id, key):
    inst, files = read(user_id, key)
    meta, body = _split_frontmatter(files["SKILL.md"].decode("utf-8"))
    from core.services.skill_management_service import extract_mcp_server_ids
    return {"id": key, "install_id": inst.install_id, "revision": inst.resolved_revision,
            "display_name": inst.payload.get("presentation", {}).get("display_name") or inst.display_name,
            "description": inst.description, "instructions": body.strip(), "tags": _list(meta.get("tags")),
            "mcp_server_ids": extract_mcp_server_ids(files["SKILL.md"].decode("utf-8")),
            "allowed_tools": _list(meta.get("allowed_tools", meta.get("allowed-tools"))), "owner": "self",
            **inst.payload.get("presentation", {}),
            "extra_files": [{"filename": n, "size": len(v), "is_binary": is_binary_value(encode_upload(n, v))}
                            for n, v in sorted(files.items()) if n != "SKILL.md"]}


def _replace(user_id, inst, files):
    with tempfile.TemporaryDirectory(prefix="skill-editor-") as tmp:
        root = Path(tmp) / "skill"
        archive.write_files(root, files)
        return lifecycle.update(user_id, inst.install_id, str(root), inst.resolved_revision)


@frontend_errors
def save(user_id, body):
    iid = registry.install_id("skill", "local", body.name)
    found = registry.get(iid)
    if getattr(body, "expected_revision", None) and (found is None or found.state == "removed"):
        from fastapi import HTTPException
        raise HTTPException(409, detail="Installation was removed; reopen the skill page")
    if found and found.state != "removed":
        inst, files = read(user_id, body.name)
        expected = getattr(body, "expected_revision", None)
        previous = store.get("skill", "local", inst.key, expected) if expected else None
        # File management can advance the package while the form is open. Only
        # accept that advancement when its SKILL.md still matches the opened form.
        if previous is None or previous.entry_file.read_bytes() != files["SKILL.md"]:
            from fastapi import HTTPException
            raise HTTPException(409, detail="技能已更新，请重新打开编辑器后保存")
        old, _ = _split_frontmatter(files["SKILL.md"].decode("utf-8"))
    else:
        inst, files, old = None, {}, {}
    # Names selected in the UI are already authorized connector identities. Runtime
    # dependency resolution remains the authority on readiness, including missing MCPs.
    from core.services.skill_management_service import extract_mcp_server_ids
    ids = body.mcp_server_ids
    if ids is None:
        ids = extract_mcp_server_ids(files.get("SKILL.md", b"").decode("utf-8"))
    files["SKILL.md"] = build_skill_content(skill_id=body.name, display_name=body.display_name,
        description=body.description, version=inst.version if inst else "1.0.0", tags=body.tags,
        allowed_tools=_list(old.get("allowed_tools", old.get("allowed-tools"))), instructions=body.instructions, mcp_server_ids=ids)
    if inst:
        result = _replace(user_id, inst, files)
    else:
        with tempfile.TemporaryDirectory(prefix="skill-editor-") as tmp:
            root = Path(tmp) / "skill"
            archive.write_files(root, files)
            result = lifecycle.install(user_id, str(root))
    lifecycle.set_presentation(user_id, result["install_id"], {
        "display_name": body.display_name, "user_intro": body.user_intro, "icon": body.icon}, result["revision"])
    return {**result, "id": result["skill_id"], "owner": "self"}


@frontend_errors
def file_get(user_id, key, filename):
    _, files = read(user_id, key)
    filename = validate_skill_file_path(filename)
    if filename == "SKILL.md" or filename not in files:
        raise ValueError("extra file not found")
    from core.agent_skills.binary_files import is_binary_value
    value = encode_upload(filename, files[filename])
    binary = is_binary_value(value)
    return {"filename": filename, "content": "" if binary else value, "is_binary": binary}


@frontend_errors
def file_change(user_id, key, filename, content=None):
    filename = validate_skill_file_path(filename)
    if filename == "SKILL.md":
        raise ValueError("edit SKILL.md using the skill form")
    inst, files = read(user_id, key)
    if content is None:
        if filename not in files:
            raise ValueError("extra file not found")
        del files[filename]
    else:
        if len(content) > 10 * 1024 * 1024:
            raise ValueError("file exceeds 10MB limit")
        files[filename] = content
    _replace(user_id, inst, files)
    return {"filename": filename, "message": "File saved" if content is not None else "File deleted"}


@frontend_errors
def export(user_id, key):
    _, files = read(user_id, key)
    from core.services.marketplace_service import build_skill_zip
    return build_skill_zip(key, files.pop("SKILL.md").decode("utf-8"), {n: encode_upload(n, v) for n, v in files.items()})


@frontend_errors
def uninstall(user_id, key):
    inst, _ = read(user_id, key)
    lifecycle.uninstall(user_id, inst.install_id, inst.resolved_revision)
    return {"skill_id": key, "deleted": True}


@frontend_errors
def icon(user_id, key, value):
    inst, _ = read(user_id, key)
    lifecycle.set_presentation(user_id, inst.install_id, {"icon": value}, inst.resolved_revision)
    return {"id": key, "icon": value}


@frontend_errors
def install_archive(user_id, raw, kind="skill"):
    if len(raw) > 50 * 1024 * 1024:
        raise ValueError("package exceeds 50MB limit")
    from core.services import local_plugin_service
    with tempfile.TemporaryDirectory(prefix="manager-upload-") as tmp:
        path = Path(tmp) / "package.zip"
        path.write_bytes(raw)
        result = (lifecycle if kind == "skill" else local_plugin_service).install(user_id, str(path))
    return {**result, "id": result.get("skill_id", result.get("slug")), "owner": "self"}
