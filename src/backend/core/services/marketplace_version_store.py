"""Version snapshots belonging to existing marketplace entries."""

import base64
import hashlib
import io
import json
import uuid
import zipfile

from core.db.models import ContentBlock
from core.infra.time import utc_now
from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

PREFIX = "market-version:"
POINTER = "market-active:"


def key(kind, slug):
    return hashlib.sha256(f"{kind}:{slug}".encode()).hexdigest()


def state(db, kind, slug):
    row = db.get(ContentBlock, POINTER + key(kind, slug)) if db is not None else None
    return row.payload if row else None


def active(db, kind, slug):
    value = state(db, kind, slug)
    if not value:
        return None
    if kind in {"skill", "agent"}:
        from . import agent_market_service as agents
        from . import marketplace_service as skills

        native = (
            skills.get_marketplace_skill(slug, db)
            if kind == "skill"
            else agents.get_agent_detail(db, slug)
        )
        if value.get("source_version", native["version"]) != native["version"]:
            return None
    if kind == "plugin":
        number, fingerprint = native_plugin_source(db, slug)
        if value.get("source_version", number) != number or (
            fingerprint and value.get("source_fingerprint", fingerprint) != fingerprint
        ):
            return None
    row = db.get(ContentBlock, PREFIX + value["active"])
    if kind == "connector" and row:
        from core.db.models import McpMarketItem, McpMarketVersion

        item = db.get(McpMarketItem, slug)
        version = db.get(McpMarketVersion, item.latest_version_id) if item else None
        if not version or version.version != row.payload["detail"]["version"]:
            return None
    return row.payload if row else None


def encode(files):
    return {name: base64.b64encode(raw).decode() for name, raw in files.items()}


def decode(snapshot):
    return {name: base64.b64decode(raw) for name, raw in snapshot["files"].items()}


def archive(files, wrapper=""):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for name, raw in sorted(files.items()):
            out.writestr(f"{wrapper}/{name}" if wrapper else name, raw)
    return buf.getvalue()


def revision(detail):
    return hashlib.sha256(json.dumps(detail, sort_keys=True, default=str).encode()).hexdigest()


def listing(db, kind, slug, detail):
    snapshot = active(db, kind, slug)
    if snapshot:
        # Presentation, visibility and installed flags stay under existing controls.
        keys = ("version", "skills_count")
        detail.update({k: snapshot["detail"][k] for k in keys if k in snapshot["detail"]})
    return detail


def revision_token(db, kind, slug, current):
    pointer = state(db, kind, slug) or {}
    return revision(
        {
            "detail": current,
            "generation": pointer.get("generation"),
            "highest_version": pointer.get("highest_version"),
        }
    )


def native_plugin_source(db, slug):
    from core.plugins.management.packages import _market_row
    from core.plugins.packaging import sources

    root = sources._resolve_plugin_dir(slug)
    if root is not None:
        meta = sources._scan_native_manifest(root)
        if not meta:
            from core.plugins.packaging.importer import normalize_plugin_dir

            return normalize_plugin_dir(root).version, None
        return meta.get("version") or "1.0.0", None
    row = _market_row(db, slug)
    if row is None:
        from core.infra.exceptions import ResourceNotFoundError

        raise ResourceNotFoundError("plugin", slug)
    return row.version or "1.0.0", hashlib.sha256(row.package_b64.encode()).hexdigest()


def versions(db, kind, slug, current):
    value = state(db, kind, slug)
    if kind == "connector":
        from core.db.models import McpMarketItem, McpMarketVersion

        rows = (
            db.query(McpMarketVersion)
            .filter_by(slug=slug)
            .order_by(McpMarketVersion.created_at.desc())
            .all()
        )
        item = db.get(McpMarketItem, slug)
        ordered = sorted(rows, key=lambda r: r.version_id != item.latest_version_id)
        highest = max((r.version for r in rows), key=version_order)
        return {
            "versions": [
                {
                    "id": "mcp:" + r.version_id,
                    "version": r.version,
                    "current": r.version_id == item.latest_version_id,
                }
                for r in ordered[:6]
            ],
            "revision": revision_token(db, kind, slug, current),
            "highest_version": highest,
        }
    records = []
    if value:
        effective = active(db, kind, slug)
        ids = [value["active"], *reversed(value["history"])]
        if not effective:
            records = [{"id": "", "version": current["version"], "current": True}]
        seen = {record["version"] for record in records}
        for identity in ids:
            snap = db.get(ContentBlock, PREFIX + identity).payload
            number = snap["detail"]["version"]
            if number in seen:
                continue
            seen.add(number)
            records.append(
                {
                    "id": identity,
                    "version": number,
                    "current": bool(effective) and identity == value["active"],
                }
            )
            if len(records) == 6:
                break
    else:
        records = [{"id": "", "version": current["version"], "current": True}]
    return {
        "versions": records,
        "revision": revision_token(db, kind, slug, current),
        "highest_version": (
            max([value["highest_version"], current["version"]], key=version_order)
            if value
            else current["version"]
        ),
    }


def save(db, kind, slug, snapshot, current, expected, target=None):
    """Atomically compare source + pointer; immutable snapshots and active content."""
    from .marketplace_version_archive import fail
    from .marketplace_version_sources import (
        activate,
        connector_snapshot,
        lock_source,
        source_detail,
    )

    # Refresh ORM state after waiting for another publication to finish.
    pointer_id = POINTER + key(kind, slug)
    db.expire_all()
    row = (
        db.query(ContentBlock)
        .filter_by(id=pointer_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    lock_source(db, kind, slug)
    actual = source_detail(db, kind, slug)
    if revision_token(db, kind, slug, actual) != expected:
        raise HTTPException(409, "市场内容已变化，请刷新详情后重试")
    old = dict(row.payload) if row else None
    history = list(old["history"]) if old else []
    highest = versions(db, kind, slug, actual)["highest_version"]
    native_number = actual.get("native_version", actual["version"])
    if not target and version_order(snapshot["detail"]["version"]) <= version_order(highest):
        raise HTTPException(409, "市场版本已变化，请刷新详情后重试")
    # Native publisher updates must also be retained before selecting an older snapshot.
    if kind != "connector" and (not old or old.get("source_version") != native_number):
        from .marketplace_version_sources import capture

        baseline = str(uuid.uuid4())
        db.add(
            ContentBlock(id=PREFIX + baseline, payload=capture(db, kind, slug), updated_by="admin")
        )
        history.append(baseline)
    if target:
        if kind == "connector" and target.startswith("mcp:"):
            snapshot = connector_snapshot(db, slug, target[4:])
            chosen = str(uuid.uuid4())
            db.add(ContentBlock(id=PREFIX + chosen, payload=snapshot, updated_by="admin"))
            history.append(chosen)
        else:
            if not old or target not in history:
                fail("所选历史版本不存在")
            snaprow = db.get(ContentBlock, PREFIX + target)
            snapshot = snaprow.payload
            chosen = target
    else:
        chosen = str(uuid.uuid4())
        db.add(ContentBlock(id=PREFIX + chosen, payload=snapshot, updated_by="admin"))
        history.append(chosen)
    payload = {
        "generation": str(uuid.uuid4()),
        "active": chosen,
        "history": history,
        "source_version": native_number,
        "source_fingerprint": native_plugin_source(db, slug)[1] if kind == "plugin" else None,
        "highest_version": highest if target else snapshot["detail"]["version"],
    }
    try:
        if row:
            changed = db.execute(
                update(ContentBlock)
                .where(
                    ContentBlock.id == pointer_id,
                    ContentBlock.payload["generation"].as_string() == old.get("generation"),
                )
                .values(payload=payload, updated_at=utc_now()),
                execution_options={"synchronize_session": False},
            ).rowcount
            if changed != 1:
                raise HTTPException(409, "市场内容已变化，请刷新详情后重试")
            db.expire(row)
        else:
            db.add(ContentBlock(id=pointer_id, payload=payload, updated_by="admin"))
        db.flush()
        activate(db, kind, slug, snapshot)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "市场内容已变化，请刷新详情后重试") from None
    except Exception:
        db.rollback()
        raise
    return versions(db, kind, slug, source_detail(db, kind, slug))


def detail(db, kind, slug, native):
    snapshot = active(db, kind, slug)
    result = dict(native)
    result["source_revision"] = revision(native)
    result["native_version"] = native["version"]
    if snapshot:
        fields = (
            "version",
            "instructions",
            "files",
            "system_prompt",
            "welcome_message",
            "suggested_questions",
            "bindings",
        )
        result.update({k: snapshot["detail"][k] for k in fields if k in snapshot["detail"]})
    return result


def version_order(number):
    import re

    match = re.fullmatch(r"[Vv]?(\d+)(?:\.(\d+))?(?:\.(\d+))?", number or "")
    if not match:
        return (0, 0, 0)
    return tuple(int(part or 0) for part in match.groups())
