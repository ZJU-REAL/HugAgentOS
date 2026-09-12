"""Device-side explicit capability changes, isolated from conversation execution."""

import hashlib
import json
import time
import uuid
from pathlib import Path
from urllib.parse import quote
import httpx
from fastapi import HTTPException
from core.db.models import ContentBlock
from core.agent_skills.binary_files import encode_upload
from . import archive, registry, skills, store
from .paths import assert_managed_path, LOCAL_PROFILE, BUILTIN_PROFILE
from .change_merge import plan, revision, validate_files, sensitive_paths
from .errors import CapabilityError
from .mcp_json import McpJsonError
from sqlalchemy.exc import SQLAlchemyError
from core.infra.crypto import encrypt_secret, decrypt_secret


def _key(prefix, user_id, profile, iid):
    return (
        prefix
        + hashlib.sha256((str(user_id) + "\0" + str(profile) + "\0" + iid).encode()).hexdigest()
    )


def _read(key):
    with registry._session() as db:
        row = db.get(ContentBlock, key)
        return json.loads(decrypt_secret(row.payload["encrypted"])) if row else None


def _save(key, payload):
    payload = {"encrypted": encrypt_secret(json.dumps(payload))}
    with registry._session() as db:
        row = db.get(ContentBlock, key)
        if row is None:
            db.add(ContentBlock(id=key, payload=payload))
        else:
            row.payload = payload


def cloud_request(state, method, kind, key, body=None):
    from core.services.desktop_cloud_bridge import cloud_headers, require_current_account

    require_current_account(state)
    url = (
        state["cloud_base"].rstrip("/")
        + "/api/v1/desktop/capability/workcopies/"
        + kind
        + "/"
        + quote(key, safe="")
    )
    try:
        response = httpx.request(method, url, headers=cloud_headers(state), json=body, timeout=30)
    except httpx.HTTPError as exc:
        raise HTTPException(502, detail="云端暂不可用，本地修改已保留") from exc
    require_current_account(state)
    if response.status_code >= 400:
        messages = {
            403: "没有提交该云端能力的权限",
            404: "云端尚未支持此同步接口，或能力不可访问",
            409: "云端版本已变化，请重新比较",
            422: "云端未接受该能力配置",
        }
        raise HTTPException(
            response.status_code,
            detail=messages.get(response.status_code, "云端提交失败，本地修改已保留"),
        )
    try:
        data = response.json()["data"]
        validate_files(data["files"])
        if data["revision"] != revision(data["files"]):
            raise ValueError("revision mismatch")
        return data
    except (KeyError, TypeError, ValueError, CapabilityError) as exc:
        raise HTTPException(502, detail="云端返回内容无法验证，本地文件已保留") from exc


def source(user_id, iid):
    try:
        kind, profile, key = iid.split(":", 2)
    except ValueError as exc:
        raise HTTPException(400, detail="能力标识无效") from exc
    if kind not in ("skill", "plugin", "mcp"):
        raise HTTPException(400, detail="此类型暂不支持文件提交")
    current = skills.current_account_profile()
    if profile not in (LOCAL_PROFILE, BUILTIN_PROFILE, "local-json", current):
        raise HTTPException(403, detail="该能力不属于当前账号")
    if profile == current and not skills.account_authorized_for(user_id):
        raise HTTPException(403, detail="账号不匹配")
    inst = registry.get(iid)
    if kind == "mcp":
        if profile == LOCAL_PROFILE:
            from core.db.models import AdminMcpServer
            from core.services.mcp_management_service import decrypt_mcp_headers

            with registry._session() as db:
                row = db.get(AdminMcpServer, key)
                if row is None or row.owner_user_id != str(user_id):
                    raise HTTPException(404, detail="本机连接器不存在或不可提交")
                spec = {
                    name: getattr(row, name)
                    for name in (
                        "display_name",
                        "description",
                        "transport",
                        "command",
                        "args",
                        "url",
                        "env_vars",
                        "extra_config",
                    )
                }
                spec["headers"] = decrypt_mcp_headers(row.headers or {})
        elif profile == "local-json":
            from .mcp_json import load
            from .paths import mcp_json_path

            doc = load()
            raw = assert_managed_path(mcp_json_path()).read_bytes()
            if hashlib.sha256(raw).hexdigest() != doc.digest:
                raise HTTPException(409, detail="连接器配置已变化，请重新比较")
            spec = json.loads(raw).get("local", {}).get("servers", {}).get(key)
        else:
            raise HTTPException(422, detail="请先建立本机连接器配置，再提交本地变更")
        if spec is None:
            raise HTTPException(404, detail="本机连接器不存在")
        return (
            kind,
            key,
            {"connector.json": json.dumps(spec, ensure_ascii=False, sort_keys=True, indent=2)},
            None,
        )
    if inst is None or inst.payload.get("owner_user_id") not in (None, "", str(user_id)):
        raise HTTPException(404, detail="能力不存在或不可访问")
    if inst.payload.get("discovered_path"):
        path = assert_managed_path(Path(inst.payload["discovered_path"]))
    else:
        comp = store.get(kind, profile, key, inst.resolved_revision or "")
        if comp is None:
            raise HTTPException(409, detail="本机文件缺失，无法比较")
        path = comp.path
    entries = [(name, file) for name, file in archive.iter_files(path) if name != ".inventory.json"]
    sizes = [file.stat().st_size for _, file in entries]
    if (
        len(entries) > archive.MAX_MEMBERS
        or sum(sizes) > archive.MAX_TOTAL_BYTES
        or any(size > archive.MAX_MEMBER_BYTES for size in sizes)
    ):
        raise HTTPException(413, detail="能力文件超过单次提交大小限制")
    files = {name: encode_upload(name, file.read_bytes()) for name, file in entries}
    return kind, inst.payload.get("cloud_install_id") or key, validate_files(files), path


def preview(user_id, iid, state):
    kind, key, files, _ = source(user_id, iid)
    profile = skills.current_account_profile()
    base = _read(_key("cap-change-base:", user_id, profile, iid))
    key = base.get("cloud_key", key) if base else key
    try:
        remote = cloud_request(state, "GET", kind, key)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        # Do not reveal another account's files; a private fork uses local bytes.
        remote = {"files": {}, "revision": revision({}), "exists": True, "can_edit": False}
    validate_files(remote["files"])
    if remote.get("revision") != revision(remote["files"]):
        raise HTTPException(502, detail="云端比较内容不完整")
    base_files = base["files"] if base else ({} if not remote.get("exists") else None)
    result = plan(base_files, files, remote["files"])
    token = uuid.uuid4().hex
    from core.services.desktop_cloud_bridge import account_scope

    with account_scope(state):
        _save(
            "cap-change-preview:" + token,
            {
                "user_id": str(user_id),
                "profile": profile,
                "install_id": iid,
                "kind": kind,
                "key": key,
                "local": files,
                "base": base_files,
                "remote": remote,
                "expires": time.time() + 1800,
            },
        )
    return {
        "preview_id": token,
        "install_id": iid,
        "changes": result["changes"],
        "local_revision": revision(files),
        "cloud_revision": remote["revision"],
        "can_edit": remote.get("can_edit", False),
        "cloud_exists": remote.get("exists", False),
        "sensitive_paths": sorted(set(sensitive_paths(files) + sensitive_paths(remote["files"]))),
    }


def _apply_local(user_id, iid, files, path, expected_revision):
    import os
    from .paths import require_root, revision_for_hash
    from core.agent_skills.binary_files import decode_binary, is_binary_value
    from core.services.desktop_capability_protocol import skill_content_hash, entity_content_hash

    kind, profile, key = iid.split(":", 2)
    if kind == "mcp":
        if profile != "local-json":
            raise HTTPException(409, detail="云端已保存，请在本机连接器编辑器确认合并配置")
        from .mcp_json import load, write, _validate_local_server
        from .paths import mcp_json_path

        spec = json.loads(files["connector.json"])
        _validate_local_server(key, spec)
        doc = load()
        raw = assert_managed_path(mcp_json_path()).read_bytes()
        if (
            hashlib.sha256(raw).hexdigest() != doc.digest
            or revision(source(user_id, iid)[2]) != expected_revision
        ):
            raise HTTPException(409, detail="连接器配置已变化，请重新比较")
        doc.local = json.loads(raw)["local"]["servers"]
        doc.local[key] = spec
        write(doc, expected_generation=doc.generation)
        return
    decoded = {
        name: decode_binary(value) if is_binary_value(value) else value
        for name, value in files.items()
    }
    digest = (
        skill_content_hash(
            files.get("SKILL.md", ""), {k: v for k, v in files.items() if k != "SKILL.md"}
        )
        if kind == "skill"
        else entity_content_hash(files)
    )
    inst = registry.get(iid)
    if inst.payload.get("discovered_path"):
        stage = assert_managed_path(path.parent / ("change-stage-" + uuid.uuid4().hex))
        backup = assert_managed_path(
            require_root() / ".capabilities" / "change-backups" / uuid.uuid4().hex
        )
        backup.parent.mkdir(parents=True, exist_ok=True)
        archive.write_files(stage, decoded)
        if os.name != "nt":
            for name in files:
                old = path / name
                if old.is_file():
                    (stage / name).chmod(old.stat().st_mode & 0o777)
        if revision(source(user_id, iid)[2]) != expected_revision:
            raise HTTPException(409, detail="本地文件已变化，保留本地版本")
        os.replace(path, backup)
        try:
            os.replace(stage, path)
        except OSError:
            os.replace(backup, path)
            raise
        from .discovery import scan

        scan(user_id)
    else:
        comp = store.write_from_files(
            kind, profile, key, revision_for_hash(digest) + "-" + uuid.uuid4().hex[:8], decoded
        )
        registry.set_state(
            iid,
            "ready",
            resolved_revision=comp.revision,
            payload_update={"content_hash": digest, "resolved_content_hash": digest},
        )
        skills.bump_view_generation()


def commit(user_id, token, choices, state, *, acknowledge_sensitive=False, fork_key=None):
    from .change_merge import resolve

    record_key = "cap-change-preview:" + token
    record = _read(record_key)
    request_hash = revision(
        {
            "request.json": json.dumps(
                {
                    "choices": choices,
                    "fork_key": fork_key,
                    "acknowledge_sensitive": acknowledge_sensitive,
                },
                sort_keys=True,
            )
        }
    )
    profile = skills.current_account_profile()
    if not record or record["user_id"] != str(user_id) or record["profile"] != profile:
        raise HTTPException(404, detail="比较记录不存在")
    if record.get("result"):
        if record.get("request_hash") != request_hash:
            raise HTTPException(409, detail="该比较已提交，请重新比较")
        return record["result"]
    if record["expires"] < time.time():
        raise HTTPException(409, detail="比较已过期，请重新比较")
    kind, key, local, path = source(user_id, record["install_id"])
    if revision(local) != revision(record["local"]):
        raise HTTPException(409, detail="确认期间本地文件已变化，请重新比较")
    try:
        files = (
            validate_files(local)
            if fork_key
            else resolve(record["base"], local, record["remote"]["files"], choices)
        )
    except (ValueError, CapabilityError) as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    if sensitive_paths(files) and not acknowledge_sensitive:
        raise HTTPException(409, detail="包含疑似敏感内容，需要明确确认上传")
    if not fork_key and record["remote"].get("exists") and not record["remote"].get("can_edit"):
        raise HTTPException(403, detail="不能覆盖原能力，请另存为自己的云端能力")
    response = cloud_request(
        state,
        "POST",
        kind,
        fork_key or record["key"],
        {
            "files": files,
            "expected_revision": None if fork_key else record["remote"]["revision"],
            "expected_model_version": None if fork_key else record["remote"].get("model_version"),
            "request_id": token,
            "acknowledge_sensitive": acknowledge_sensitive,
            "create_only": bool(fork_key or not record["remote"].get("exists")),
        },
    )
    if (
        response.get("revision") != revision(response.get("files", {}))
        or response.get("files") != files
    ):
        raise HTTPException(502, detail="云端提交回执无法验证，请重新比较后确认状态")
    result = {
        "uploaded": True,
        "local_applied": False,
        "cloud_key": response.get("key", fork_key or key),
        "revision": response["revision"],
        "applied": response.get("applied", True),
    }
    # A remote success is never retried as a new write merely because local
    # reconciliation failed. Keep its receipt and leave newer local edits alone.
    from core.services.desktop_cloud_bridge import account_scope

    with account_scope(state):
        record["result"] = result
        record["request_hash"] = request_hash
        _save(record_key, record)
        _save(
            _key("cap-change-base:", user_id, profile, record["install_id"]),
            {
                "files": response["files"],
                "revision": response["revision"],
                "cloud_key": response.get("key", fork_key or record["key"]),
            },
        )
        if not fork_key:
            try:
                _, _, latest, _ = source(user_id, record["install_id"])
                if revision(latest) == revision(local):
                    _apply_local(
                        user_id, record["install_id"], response["files"], path, revision(local)
                    )
                    result["local_applied"] = True
            except (OSError, ValueError, HTTPException, CapabilityError, McpJsonError):
                pass
        record["result"] = result
        _save(record_key, record)
    return result


def annotate(user_id, listing):
    """Local-only change hints. Network comparison happens only on explicit preview."""
    profile = skills.current_account_profile()
    for item in listing["items"]:
        iid = item["install_id"]
        item["change_state"] = "unavailable"
        try:
            kind, _, files, _ = source(user_id, iid)
            base = _read(_key("cap-change-base:", user_id, profile, iid))
            if base:
                item["change_state"] = (
                    "synced" if revision(files) == base["revision"] else "modified"
                )
            elif item["source"] == "local":
                item["change_state"] = "new"
            else:
                inst = registry.get(iid)
                if kind == "skill" and inst:
                    from core.services.desktop_capability_protocol import skill_content_hash

                    digest = skill_content_hash(
                        files.get("SKILL.md", ""),
                        {k: v for k, v in files.items() if k != "SKILL.md"},
                    )
                    item["change_state"] = "modified" if digest != inst.content_hash else "compare"
                elif kind == "plugin" and inst:
                    from core.services.desktop_capability_protocol import entity_content_hash

                    item["change_state"] = (
                        "modified" if entity_content_hash(files) != inst.content_hash else "compare"
                    )
                else:
                    item["change_state"] = "compare"
        except (
            OSError,
            ValueError,
            TypeError,
            HTTPException,
            CapabilityError,
            McpJsonError,
            SQLAlchemyError,
        ):
            pass
    return listing


def record_downloaded(inst):
    """Called only after package bytes match the authenticated manifest."""
    import logging

    try:
        user_id = skills.current_local_user_id()
        if not user_id:
            return
        _, key, files, _ = source(user_id, inst.install_id)
        _save(
            _key("cap-change-base:", user_id, inst.profile_id, inst.install_id),
            {"files": files, "revision": revision(files), "cloud_key": key},
        )
    except (OSError, ValueError, TypeError, HTTPException, CapabilityError, SQLAlchemyError):
        # Baseline capture is optional bookkeeping, never a runtime dependency.
        logging.getLogger(__name__).warning("capability ready; change baseline unavailable")
