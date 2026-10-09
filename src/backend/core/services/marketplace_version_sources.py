"""Read and activate versioned marketplace sources, never installed copies."""

import json
import tempfile
from pathlib import Path

from core.agent_skills.binary_files import encode_upload
from core.agent_skills.registry import _load_skill_metadata_from_str, _split_frontmatter
from core.db.models import AdminSkill, McpMarketItem, McpMarketVersion
from fastapi import HTTPException

from . import marketplace_version_store as store
from .marketplace_version_archive import fail, read_zip, skill_version, strip_wrapper


def source_detail(db, kind, slug):
    from core.plugins.management.details import get_plugin_detail

    from . import marketplace_agent_versions as agents
    from . import marketplace_skill_versions as skills
    from . import mcp_marketplace_service as mcps

    if kind == "skill":
        return skills.get_marketplace_skill(slug, db)
    if kind == "agent":
        return agents.get_agent_detail(db, slug)
    if kind == "plugin":
        detail = get_plugin_detail(slug, db)
        detail["native_version"], detail["source_revision"] = store.native_plugin_source(db, slug)
        return detail
    return mcps.get_market_item(db, slug, owner_user_id=None, viewer_user_id=None, admin=True)


def capture(db, kind, slug):
    from . import marketplace_agent_versions as agents
    from . import marketplace_export as export
    from . import mcp_marketplace_service as mcps

    detail = source_detail(db, kind, slug)
    existing = store.active(db, kind, slug)
    if existing and existing["detail"]["version"] == detail["version"]:
        return existing
    detail = source_detail(db, kind, slug)
    if kind in {"skill", "plugin"}:
        files = strip_wrapper(read_zip(getattr(export, "export_" + kind)(db, slug)), kind)
    elif kind == "agent":
        entry = agents._resolve_market_entry(db, slug)
        files = {
            "agent.json": json.dumps(
                {"slug": slug, **entry, "version": detail["version"]}, ensure_ascii=False
            ).encode()
        }
    else:
        item = db.get(McpMarketItem, slug)
        version = mcps._version_for_item(db, item)
        config = {
            k: getattr(version, k)
            for k in (
                "transport",
                "url",
                "auth_schema",
                "auth_config",
                "tools_json",
                "tool_hash",
                "listing_notice",
                "source_server_id",
            )
        }
        files = {
            "connector.json": json.dumps(
                {"slug": slug, **config, "version": detail["version"]}, ensure_ascii=False
            ).encode()
        }
    return {"detail": detail, "files": store.encode(files)}


def normalized_plugin(files):
    from core.plugins.packaging.importer import normalize_plugin_dir

    with tempfile.TemporaryDirectory(prefix="market-version-") as directory:
        root = Path(directory)
        for name, data in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return normalize_plugin_dir(root)


def prepare(db, kind, slug, raw, number):
    old = capture(db, kind, slug)
    detail = dict(old["detail"])
    files = read_zip(raw)
    if kind in {"skill", "plugin"}:
        files = strip_wrapper(files, kind)
    if kind == "skill":
        try:
            content = skill_version(files["SKILL.md"].decode(), number)
            meta = _load_skill_metadata_from_str(content, detail["entry_name"])
        except (ValueError, KeyError, UnicodeError):
            fail("SKILL.md 格式无效")
        if meta.id != detail["entry_name"]:
            fail("技能 ID 与当前市场资源不一致")
        from core.ontology.build_validator import ensure_ontology_build_valid

        ensure_ontology_build_valid(
            db,
            asset_type="skill",
            name=meta.name,
            description=meta.description,
            instructions=content,
            tool_names=meta.allowed_tools,
            ontology_tags=meta.tags,
        )
        files["SKILL.md"] = content.encode()
        if "marketplace.json" in files:
            manifest = json.loads(files["marketplace.json"])
            if not isinstance(manifest, dict):
                fail("市场清单必须是 JSON 对象")
            if manifest.get("entry_name") != detail["entry_name"]:
                fail("市场清单资源 ID 不一致")
            manifest["version"] = number
            files["marketplace.json"] = json.dumps(manifest, ensure_ascii=False).encode()
        detail.update(
            instructions=_split_frontmatter(content)[1].strip(),
            summary=meta.description,
            tags=meta.tags,
            files=[
                {"path": n, "size": len(v)}
                for n, v in files.items()
                if n not in {"SKILL.md", "marketplace.json"}
            ],
        )
    elif kind == "plugin":
        manifests = [
            n
            for n in ("plugin.json", ".claude-plugin/plugin.json", ".codex-plugin/plugin.json")
            if n in files
        ]
        if len(manifests) != 1:
            fail("插件只能包含一份清单")
        manifest = json.loads(files[manifests[0]])
        if not isinstance(manifest, dict):
            fail("插件清单必须是对象")
        manifest["version"] = number
        files[manifests[0]] = json.dumps(manifest, ensure_ascii=False).encode()
        for name, data in list(files.items()):
            if name == "SKILL.md" or name.endswith("/SKILL.md"):
                try:
                    files[name] = skill_version(data.decode(), number).encode()
                except UnicodeError:
                    fail("SKILL.md 必须使用 UTF-8")
        np = normalized_plugin(files)
        if np.slug != slug or np.dropped:
            fail("插件 ID 不一致或包内包含无效组件")
        from core.plugins.management.details import _normalized_to_detail

        detail.update(_normalized_to_detail(np))
        detail["skills_count"] = len(np.skills)
    else:
        name = "agent.json" if kind == "agent" else "connector.json"
        if len(files) != 1 or name not in files:
            fail(f"ZIP 必须只包含 {name}")
        try:
            config = json.loads(files[name])
        except ValueError:
            fail("配置 JSON 无效")
        if not isinstance(config, dict) or config.get("slug") != slug:
            fail("配置 slug 与当前市场资源不一致")
        from .marketplace_version_config import validate

        config = validate(db, kind, config, old)
        config["version"] = number
        files[name] = json.dumps(config, ensure_ascii=False).encode()
        if kind == "agent":
            detail.update(
                {
                    k: config[k]
                    for k in (
                        "name",
                        "description",
                        "system_prompt",
                        "welcome_message",
                        "suggested_questions",
                        "bindings",
                    )
                }
            )
        else:
            detail.update(
                transport=config["transport"],
                tools=config["tools_json"],
                tool_count=len(config["tools_json"]),
                tool_hash=config["tool_hash"],
            )
    detail["version"] = number
    return {"detail": detail, "files": store.encode(files)}


def activate(db, kind, slug, snapshot):
    """MCP uses its native immutable version rows; builtin skills use DB overrides."""
    if kind == "connector":
        config = json.loads(store.decode(snapshot)["connector.json"])
        row = (
            db.query(McpMarketVersion)
            .filter_by(slug=slug, version=snapshot["detail"]["version"])
            .first()
        )
        if row is None:
            import uuid

            row = McpMarketVersion(
                version_id=str(uuid.uuid4()),
                slug=slug,
                version=snapshot["detail"]["version"],
                **{k: v for k, v in config.items() if k not in {"slug", "version"}},
            )
            db.add(row)
        db.get(McpMarketItem, slug).latest_version_id = row.version_id
    elif kind == "skill" and snapshot["detail"].get("builtin"):
        detail = snapshot["detail"]
        identity = detail["entry_name"]
        row = db.get(AdminSkill, identity)
        if row and (row.owner_user_id is not None or row.source_plugin):
            raise HTTPException(409, "内置技能 ID 与其他资源冲突")
        content = store.decode(snapshot)
        metadata = _load_skill_metadata_from_str(content["SKILL.md"].decode(), identity)
        if row is None:
            row = AdminSkill(skill_id=identity, owner_user_id=None, is_enabled=True)
            db.add(row)
        secrets = {
            k: v
            for k, v in (row.extra_files or {}).items()
            if Path(k).name in {"secrets.json", "credentials.json"}
        }
        row.skill_content = content["SKILL.md"].decode()
        row.version = detail["version"]
        row.display_name = detail["display_name"]
        row.description = metadata.description
        row.tags, row.allowed_tools = metadata.tags, metadata.allowed_tools
        row.extra_files = {
            **{
                n: encode_upload(n, v)
                for n, v in content.items()
                if n not in {"SKILL.md", "marketplace.json"}
            },
            **secrets,
        }


def connector_snapshot(db, slug, version_id):
    from . import mcp_marketplace_service as mcps

    item = db.get(McpMarketItem, slug)
    version = db.get(McpMarketVersion, version_id)
    if not item or not version or version.slug != slug:
        fail("所选历史版本不存在")
    detail = mcps._item_dict(db, item, version, installed=False)
    config = {
        k: getattr(version, k)
        for k in (
            "transport",
            "url",
            "auth_schema",
            "auth_config",
            "tools_json",
            "tool_hash",
            "listing_notice",
            "source_server_id",
        )
    }
    return {
        "detail": detail,
        "files": store.encode(
            {
                "connector.json": json.dumps(
                    {"slug": slug, **config, "version": detail["version"]}, ensure_ascii=False
                ).encode()
            }
        ),
    }


def lock_source(db, kind, slug):
    from core.db.models import AgentMarketSubmission, MarketplaceSubmission, PluginMarketPackage

    models = {
        "skill": MarketplaceSubmission,
        "agent": AgentMarketSubmission,
        "plugin": PluginMarketPackage,
        "connector": McpMarketItem,
    }
    model = models[kind]
    db.query(model).filter_by(slug=slug).populate_existing().with_for_update().first()
    if kind == "connector":
        item = db.get(McpMarketItem, slug)
        if item:
            db.query(McpMarketVersion).filter_by(
                version_id=item.latest_version_id
            ).populate_existing().with_for_update().first()
