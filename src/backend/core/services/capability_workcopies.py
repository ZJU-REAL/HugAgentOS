"""Account-owned cloud revisions with compare-and-swap and durable receipts."""

import hashlib
import json
import threading
import logging
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from datetime import datetime, timezone
from fastapi import HTTPException
from core.db.engine import SessionLocal
from core.db.models import AdminSkill, AdminMcpServer, InstalledPlugin, ContentBlock
from core.infra.crypto import encrypt_secret, decrypt_secret
from core.capabilities.change_merge import revision, validate_files, sensitive_paths
from core.capabilities.paths import safe_segment
from core.capabilities.errors import CapabilityError

_lock = threading.RLock()


def _id(prefix, user, kind, key):
    return prefix + hashlib.sha256((str(user) + "\0" + kind + "\0" + key).encode()).hexdigest()


def _model(db, kind, key):
    models = {
        "skill": (AdminSkill, AdminSkill.skill_id),
        "plugin": (InstalledPlugin, InstalledPlugin.install_id),
        "mcp": (AdminMcpServer, AdminMcpServer.server_id),
    }
    if kind not in models:
        raise HTTPException(400, detail="unsupported capability kind")
    model, pk = models[kind]
    return db.query(model).filter(pk == key).with_for_update().first()


def _files(row, kind):
    if row is None:
        return {}
    if kind == "skill":
        return {"SKILL.md": row.skill_content, **dict(row.extra_files or {})}
    if kind == "plugin":
        data = {
            key: getattr(row, key)
            for key in ("install_id", "slug", "name", "version", "description", "category", "icon")
        }
        data["components"] = row.component_ids or {}
        data["ui_contributions"] = row.ui_contributions
        data["import_report"] = row.import_report or {}
        from core.services.desktop_capability import _plugin_files

        return _plugin_files(data)
    # Existing cloud credentials are never exported to a device.
    config = {key: getattr(row, key) for key in ("display_name", "description", "transport", "url")}
    return {"connector.json": json.dumps(config, ensure_ascii=False, sort_keys=True, indent=2)}


def _stored(db, user, kind, key):
    record = db.get(ContentBlock, _id("cap-cloud-copy:", user, kind, key))
    if record is None:
        return None
    text = decrypt_secret(record.payload.get("encrypted"))
    if text is None:
        raise HTTPException(409, detail="saved revision cannot be decrypted")
    return json.loads(text)


def _snapshot(db, user, kind, key):
    row = _model(db, kind, key)
    if row is not None and row.owner_user_id not in (None, str(user)):
        raise HTTPException(404, detail="capability not available")
    if row is not None and row.owner_user_id is None:
        from core.services import desktop_capability as capabilities

        if kind == "skill":
            allowed = {
                item["skill_id"]
                for item in capabilities.build_user_skill_manifest(user, use_cache=False)["skills"]
            }
        elif kind == "plugin":
            allowed = {
                item["install_id"]
                for item in capabilities.build_user_plugin_manifest(user, use_cache=False)[
                    "entries"
                ]
            }
        else:
            allowed = set(capabilities._user_effective_configs(user, use_cache=False)[0])
        if key not in allowed:
            raise HTTPException(404, detail="capability not available")
    current = _files(row, kind)
    stored = _stored(db, user, kind, key)
    files = current
    if stored:
        files = (
            stored["files"]
            if stored["model_revision"] == revision(current)
            else current if kind == "skill" else {**stored["files"], **current}
        )
    model_version = hashlib.sha256(
        json.dumps(
            (
                {column.name: str(getattr(row, column.name)) for column in row.__table__.columns}
                if row is not None
                else {}
            ),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return {
        "key": key,
        "files": files,
        "revision": revision(files),
        "model_version": model_version,
        "exists": bool(row or stored),
        "can_edit": row is None or row.owner_user_id == str(user),
        "applied": stored.get("applied", True) if stored else True,
    }, row


def snapshot(user, kind, key):
    with SessionLocal() as db:
        return _snapshot(db, str(user), kind, key)[0]


def _activate(db, user, kind, key, files, row):
    """Apply supported declarations; unsupported runtimes remain saved revisions."""
    from api.routes.v1.me_capabilities import _require_flag

    flags = {
        "skill": ("can_add_skill", "自助添加技能"),
        "plugin": ("can_import_plugin", "导入插件"),
        "mcp": ("can_add_mcp", "自助添加 MCP"),
    }
    _require_flag(str(user), db, *flags[kind])
    if kind == "skill":
        from core.agent_skills.registry import _load_skill_metadata_from_str

        content = files.get("SKILL.md", "")
        metadata = _load_skill_metadata_from_str(content, key)
        from core.ontology.build_validator import ensure_ontology_build_valid
        from api.routes.v1.me_capabilities import extract_mcp_server_ids, resolve_mcp_bindings

        mcp_ids = extract_mcp_server_ids(content)
        resolve_mcp_bindings(db, mcp_ids, owner_user_id=str(user), strict=True)
        ensure_ontology_build_valid(
            db,
            asset_type="skill",
            name=metadata.name,
            description=metadata.description,
            instructions=content,
            tool_names=list(metadata.allowed_tools),
            mcp_server_ids=mcp_ids,
            ontology_tags=list(metadata.tags),
        )
        if row is None:
            row = AdminSkill(skill_id=key, owner_user_id=str(user), created_by=str(user))
            db.add(row)
        row.skill_content, row.display_name, row.description = (
            content,
            metadata.name,
            metadata.description,
        )
        row.version, row.tags, row.allowed_tools = (
            metadata.version,
            metadata.tags,
            metadata.allowed_tools,
        )
        row.extra_files = {name: value for name, value in files.items() if name != "SKILL.md"}
        from core.agent_skills.deps_detector import detect_dependencies
        from core.agent_skills.binary_files import is_binary_value

        row.dependencies = detect_dependencies(
            {name: value for name, value in row.extra_files.items() if not is_binary_value(value)}
        )
        return row, True
    if kind == "plugin":
        definition = json.loads(files["plugin.json"])
        if not isinstance(definition, dict):
            raise ValueError("plugin definition must be an object")
        if row is None:
            # Component installation is a separate validated operation.
            return None, False
        expected = json.loads(_files(row, kind)["plugin.json"])
        display_fields = {"name", "description", "version", "category", "icon"}
        if {k: v for k, v in definition.items() if k not in display_fields} != {
            k: v for k, v in expected.items() if k not in display_fields
        }:
            return row, False
        for field in ("name", "description", "version", "category", "icon"):
            if field in definition:
                value = definition[field]
                limit = getattr(row.__table__.columns[field].type, "length", None)
                if value is not None and (
                    not isinstance(value, str) or (limit and len(value) > limit)
                ):
                    raise ValueError("invalid plugin metadata")
                setattr(row, field, definition[field])
        return row, True
    spec = json.loads(files["connector.json"])
    if not isinstance(spec, dict):
        raise ValueError("connector definition must be an object")
    if spec.get("transport", "stdio") not in ("sse", "streamable_http") or spec.get(
        "credentialRef"
    ):
        return row, False
    import asyncio
    from core.config.settings import settings
    from core.services.mcp_management_service import validate_remote_mcp_url, encrypt_mcp_headers

    asyncio.run(
        validate_remote_mcp_url(
            spec.get("url", ""),
            allow_private_network=settings.server.mcp_self_service_allow_private_network,
            require_https=False,
        )
    )
    if row is None:
        row = AdminMcpServer(
            server_id=key,
            owner_user_id=str(user),
            created_by=str(user),
            display_name=spec.get("display_name") or key,
            is_stable=False,
        )
        db.add(row)
    for field in ("display_name", "description", "transport", "url"):
        if field in spec:
            setattr(row, field, spec[field])
    if "headers" in spec:
        row.headers = encrypt_mcp_headers(spec["headers"])
    row.tools_json = []
    return row, True


def commit(user, kind, key, body):
    user = str(user)
    try:
        files = validate_files(body["files"])
    except (ValueError, TypeError, CapabilityError) as exc:
        raise HTTPException(422, detail="invalid capability files") from exc
    if sensitive_paths(files) and not body.get("acknowledge_sensitive"):
        raise HTTPException(409, detail="sensitive upload needs confirmation")
    request_id = body["request_id"]
    receipt_key = _id("cap-cloud-receipt:", user, kind, request_id)
    request_hash = revision({"request.json": json.dumps({"key": key, **body}, sort_keys=True)})
    with _lock, SessionLocal() as db:
        try:
            # Serialize receipts and first creation across workers, not only threads.
            if db.get_bind().dialect.name == "postgresql":
                lock_id = int.from_bytes(
                    hashlib.sha256((user + kind + key).encode()).digest()[:8], "big", signed=True
                )
                db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id})
            elif db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            receipt = db.get(ContentBlock, receipt_key)
            if receipt:
                if receipt.payload["request_hash"] != request_hash:
                    raise HTTPException(
                        409, detail="request identity reused with different content"
                    )
                return json.loads(decrypt_secret(receipt.payload["encrypted"]))
            before, row = _snapshot(db, user, kind, key)
            if not before["can_edit"]:
                raise HTTPException(403, detail="create a private copy instead")
            if body.get("create_only"):
                if before["exists"]:
                    raise HTTPException(409, detail="destination already exists")
                safe_segment(key)
            elif before["revision"] != body.get("expected_revision"):
                raise HTTPException(409, detail="cloud revision changed")
            elif row is not None and before["model_version"] != body.get("expected_model_version"):
                raise HTTPException(409, detail="cloud configuration changed")
            previous_copy = _stored(db, user, kind, key)
            row, applied = _activate(db, user, kind, key, files, row)
            db.flush()
            data = {
                "files": files,
                "model_revision": revision(_files(row, kind)),
                "applied": applied,
            }
            if kind == "plugin":
                previous_active = (previous_copy or {}).get("active")
                if previous_active is None and previous_copy and previous_copy.get("applied"):
                    previous_active = {
                        "files": previous_copy["files"],
                        "model_revision": previous_copy["model_revision"],
                    }
                data["active"] = (
                    {"files": files, "model_revision": data["model_revision"]}
                    if applied
                    else previous_active
                )
            storage_key = _id("cap-cloud-copy:", user, kind, key)
            record = db.get(ContentBlock, storage_key)
            if record is None:
                record = ContentBlock(id=storage_key)
                db.add(record)
            record.payload = {
                "encrypted": encrypt_secret(json.dumps(data)),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            for version in (before["files"], files):
                history_key = _id("cap-cloud-version:", user, kind, key + ":" + revision(version))
                if db.get(ContentBlock, history_key) is None:
                    db.add(
                        ContentBlock(
                            id=history_key,
                            payload={
                                "encrypted": encrypt_secret(json.dumps(version)),
                                "created_at": datetime.now(timezone.utc).isoformat(),
                            },
                        )
                    )
            result = {"key": key, "files": files, "revision": revision(files), "applied": applied}
            db.add(
                ContentBlock(
                    id=receipt_key,
                    payload={
                        "request_hash": request_hash,
                        "encrypted": encrypt_secret(json.dumps(result)),
                    },
                )
            )
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(409, detail="destination changed; compare again") from exc
        except (ValueError, KeyError, TypeError, CapabilityError) as exc:
            db.rollback()
            raise HTTPException(422, detail="invalid capability files") from exc
    from core.agent_skills.cache_refresh import refresh_skill_caches
    from core.services.mcp_management_service import refresh_mcp_caches

    if applied:
        try:
            (refresh_mcp_caches if kind == "mcp" else refresh_skill_caches)()
        except Exception:
            logging.getLogger(__name__).warning("capability saved; cache refresh pending")
    return result


def active_plugin_files(db, user, row):
    """Only validated active uploads join the normal plugin download protocol."""
    if row.owner_user_id != str(user):
        return None
    stored = _stored(db, user, "plugin", row.install_id)
    if not stored:
        return None
    active = stored.get("active")
    if active is None and stored.get("applied"):
        active = stored
    if active is None:
        return None
    current = _files(row, "plugin")
    return (
        active["files"]
        if active["model_revision"] == revision(current)
        else {**active["files"], **current}
    )
