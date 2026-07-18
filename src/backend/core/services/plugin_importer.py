"""Plugin manifest detection + normalization (unified reading layer for native / Claude Code / Codex packages).

This module is **only responsible for reading a plugin directory into a unified
NormalizedPlugin** (detecting the manifest format, auto-discovering skills and
MCP, rewriting path variables, grading by the three-tier portability matrix);
it does not write to the database. Persistence is handled by ``plugin_service``.

All three vendors anchor on the same open standards (Agent Skills SKILL.md + MCP), so:
- 🟢 Tier1 direct import: ``skills/<id>/SKILL.md`` (incl. references/scripts/assets), remote MCP (with url)
- 🟡 Tier2 adaptation: stdio MCP (rewrite path variables + mark runtime-required; installed disabled by default), userConfig→required_secrets
- 🔴 Tier3 drop with warning: hooks (this platform has no hook runtime), subagents, commands, output-styles/themes/LSP/monitors, Codex .app.json

See internal design docs §10 for details.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.agent_skills.binary_files import pack_directory
from core.infra.exceptions import BadRequestError

logger = logging.getLogger(__name__)

SKILL_MD_NAME = "SKILL.md"


# ── Unified intermediate representation ──────────────────────────────────────

@dataclass
class NormalizedSkill:
    name: str                       # original skill name (from directory name / frontmatter)
    skill_content: str              # SKILL.md source text (path variables already rewritten)
    extra_files: Dict[str, str]     # {relative path: content} (text verbatim / binary base64)


@dataclass
class NormalizedMcp:
    name: str
    display_name: str
    description: str
    transport: str                  # stdio | streamable_http | sse
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    url: Optional[str] = None
    env_vars: Dict[str, str] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)
    needs_runtime: bool = False     # stdio → True: installed but disabled by default; enable only once the runtime is in place
    note: str = ""
    tools: List[Dict[str, Any]] = field(default_factory=list)  # tool list the manifest may declare (display only, [{name,description}])


@dataclass
class NormalizedPlugin:
    slug: str
    name: str
    version: str
    description: str
    category: str
    icon: Optional[str]
    kind: str                       # native | claude | codex
    required_secrets: List[Dict[str, Any]]
    default_enabled: Dict[str, List[str]]   # {"skills":[...], "mcp":[...]}
    skills: List[NormalizedSkill]
    mcp: List[NormalizedMcp]
    dropped: List[Dict[str, str]]   # [{type, name, reason}]
    # Admin-level config (provider credentials): filled in centrally by the admin
    # on the plugin detail page, stored in SystemConfig, shared by all users and
    # read-only on the user side. Shape: {"mode":"any|all", "group":..., "hint":...,
    # "fields":[{"key","label","secret","description"}]}. None = this plugin needs no admin config.
    admin_config: Optional[Dict[str, Any]] = None
    # Account connection type (per-user OAuth device flow): e.g. "dingtalk" / "lark".
    # When non-empty, the frontend renders the corresponding account-connection
    # panel on the plugin detail page where the user completes a one-time
    # authorization. None = no account connection needed.
    connection: Optional[str] = None


# ── Manifest detection ────────────────────────────────────────────────────────

def detect_manifest(plugin_dir: Path) -> Tuple[str, Path]:
    """Detect the plugin package type; returns (kind, manifest_path).

    kind: 'claude' | 'codex' | 'native'; raises 400 if none is present.
    """
    cc = plugin_dir / ".claude-plugin" / "plugin.json"
    cx = plugin_dir / ".codex-plugin" / "plugin.json"
    native = plugin_dir / "plugin.json"
    if cc.is_file():
        return "claude", cc
    if cx.is_file():
        return "codex", cx
    if native.is_file():
        return "native", native
    raise BadRequestError(
        message="不是有效的插件包：缺少 plugin.json / .claude-plugin/plugin.json / .codex-plugin/plugin.json"
    )


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise BadRequestError(message=f"清单 JSON 解析失败：{path.name}（{exc}）")
    if not isinstance(data, dict):
        raise BadRequestError(message=f"清单格式错误：{path.name} 必须是对象")
    return data


# ── Path variable rewriting ──────────────────────────────────────────────────

def _rewrite_path_vars(text: str, *, skill_sandbox_dir: Optional[str] = None,
                       plugin_sandbox_dir: str = "/workspace/plugins") -> str:
    """Rewrite CC/Codex path variables to this platform's sandbox paths.

    ${CLAUDE_PLUGIN_ROOT} / ${CODEX_PLUGIN_ROOT} → skill sandbox directory (in skill context) or the plugin directory
    ${CLAUDE_PLUGIN_DATA} → <root>/.data
    ${CLAUDE_PROJECT_DIR}  → /workspace
    ${user_config.X}       → ${X} (environment variable / secret)
    ${ENV_VAR}             → kept as-is
    """
    root = skill_sandbox_dir or plugin_sandbox_dir
    out = text
    out = out.replace("${CLAUDE_PLUGIN_ROOT}", root).replace("${CODEX_PLUGIN_ROOT}", root)
    out = out.replace("${CLAUDE_PLUGIN_DATA}", f"{root}/.data").replace("${CODEX_PLUGIN_DATA}", f"{root}/.data")
    out = out.replace("${CLAUDE_PROJECT_DIR}", "/workspace").replace("${CODEX_PROJECT_DIR}", "/workspace")
    # ${user_config.api_token} → ${api_token}
    out = re.sub(r"\$\{user_config\.([A-Za-z0-9_]+)\}", r"${\1}", out)
    return out


# ── Skill auto-discovery ─────────────────────────────────────────────────────

def _load_skill_dir(skill_dir: Path) -> Tuple[str, Dict[str, str]]:
    """Read one skill directory: returns (SKILL.md source text, {relative path: stored content})."""
    skill_content = (skill_dir / SKILL_MD_NAME).read_text(encoding="utf-8")
    extra_files = pack_directory(skill_dir, skip_names={SKILL_MD_NAME})
    return skill_content, extra_files


def _discover_skills(plugin_dir: Path) -> List[NormalizedSkill]:
    """Scan skills/*/SKILL.md (common to all three vendors), plus a root-level single-skill SKILL.md."""
    out: List[NormalizedSkill] = []
    skills_root = plugin_dir / "skills"
    if skills_root.is_dir():
        for child in sorted(skills_root.iterdir()):
            if child.is_dir() and (child / SKILL_MD_NAME).is_file():
                content, extra = _load_skill_dir(child)
                out.append(NormalizedSkill(name=child.name, skill_content=content, extra_files=extra))
    # Root-level single skill (CC allows a SKILL.md directly at the plugin root)
    root_md = plugin_dir / SKILL_MD_NAME
    if root_md.is_file():
        content, extra = _load_skill_dir(plugin_dir)
        # The root-level scan also sweeps subdirectories like skills/ into extra_files; strip out the skill directories already handled separately
        extra = {k: v for k, v in extra.items() if not k.startswith("skills/")
                 and not k.startswith(".claude-plugin/") and not k.startswith(".codex-plugin/")
                 and k != "plugin.json" and not k.startswith("mcp/")}
        out.append(NormalizedSkill(name=plugin_dir.name, skill_content=content, extra_files=extra))
    return out


# ── MCP auto-discovery ───────────────────────────────────────────────────────

def _find_mcp_map(plugin_dir: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Merge all MCP definition sources: root .mcp.json, mcp/servers.json, and mcpServers inside the manifest."""
    merged: Dict[str, Any] = {}

    def _merge(obj: Any) -> None:
        if not isinstance(obj, dict):
            return
        inner = obj.get("mcpServers") if isinstance(obj.get("mcpServers"), dict) else obj
        if isinstance(inner, dict):
            for k, v in inner.items():
                if isinstance(v, dict):
                    merged[str(k)] = v

    # 1) Root .mcp.json (CC / Codex)
    p = plugin_dir / ".mcp.json"
    if p.is_file():
        try:
            _merge(json.loads(p.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            logger.warning("plugin .mcp.json broken: %s", exc)
    # 2) native: mcp/servers.json (array form [{server_id, ...}])
    p2 = plugin_dir / "mcp" / "servers.json"
    if p2.is_file():
        try:
            arr = json.loads(p2.read_text(encoding="utf-8"))
            if isinstance(arr, list):
                for item in arr:
                    if isinstance(item, dict) and item.get("server_id"):
                        merged[str(item["server_id"])] = item
        except Exception as exc:  # noqa: BLE001
            logger.warning("plugin mcp/servers.json broken: %s", exc)
    # 3) Inline mcpServers in the manifest (path strings are not resolved; only inline objects are supported)
    inline = manifest.get("mcpServers")
    if isinstance(inline, dict):
        _merge(inline)
    return merged


def _normalize_mcp_entry(name: str, raw: Dict[str, Any]) -> NormalizedMcp:
    """Single MCP definition → NormalizedMcp (incl. transport inference + path variable rewriting)."""
    url = raw.get("url")
    command = raw.get("command")
    raw_type = str(raw.get("type") or raw.get("transport") or "").lower()

    if url:
        transport = "sse" if raw_type == "sse" else "streamable_http"
        needs_runtime = False
        note = ""
    else:
        transport = "stdio"
        needs_runtime = True
        note = "stdio MCP：需运行时（node/python 等）+ 文件物化，默认装上即禁用"

    env_vars = dict(raw.get("env") or raw.get("env_vars") or {})
    headers = dict(raw.get("headers") or {})
    args = list(raw.get("args") or [])

    # Path variable rewriting (text values of command/args/url/env/headers)
    plugin_dir_ph = f"/workspace/plugins/{name}"
    if command:
        command = _rewrite_path_vars(str(command), plugin_sandbox_dir=plugin_dir_ph)
    args = [_rewrite_path_vars(str(a), plugin_sandbox_dir=plugin_dir_ph) for a in args]
    if url:
        url = _rewrite_path_vars(str(url), plugin_sandbox_dir=plugin_dir_ph)
    env_vars = {k: _rewrite_path_vars(str(v), plugin_sandbox_dir=plugin_dir_ph) for k, v in env_vars.items()}
    headers = {k: _rewrite_path_vars(str(v), plugin_sandbox_dir=plugin_dir_ph) for k, v in headers.items()}

    return NormalizedMcp(
        name=name,
        display_name=str(raw.get("display_name") or name),
        description=str(raw.get("description") or ""),
        transport=transport,
        command=command,
        args=args,
        url=url,
        env_vars=env_vars,
        headers=headers,
        needs_runtime=needs_runtime,
        note=note,
        tools=[
            {"name": str(t.get("name") or ""), "description": str(t.get("description") or "")}
            for t in (raw.get("tools") or [])
            if isinstance(t, dict) and t.get("name")
        ],
    )


# ── userConfig / required_secrets normalization ──────────────────────────────

def _normalize_required_secrets(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalize native required_secrets / CC userConfig / Codex userConfig into
    [{key, label, required}] (the shape marketplace _inject_secrets expects).
    """
    out: List[Dict[str, Any]] = []
    # native: ["api_key", ...] or [{key,label,required}]
    rs = manifest.get("required_secrets")
    if isinstance(rs, list):
        for item in rs:
            if isinstance(item, str):
                out.append({"key": item, "label": item, "required": True})
            elif isinstance(item, dict) and item.get("key"):
                out.append({
                    "key": str(item["key"]),
                    "label": str(item.get("label") or item["key"]),
                    "required": bool(item.get("required", True)),
                })
    # CC / Codex: userConfig = {key: {title, sensitive, required}}
    uc = manifest.get("userConfig")
    if isinstance(uc, dict):
        for key, spec in uc.items():
            if not isinstance(spec, dict):
                continue
            # Only sensitive items are collected as credentials (non-sensitive ones could later become env vars; the MVP treats everything as credentials)
            out.append({
                "key": str(key),
                "label": str(spec.get("title") or key),
                "required": bool(spec.get("required", False)),
            })
    # Deduplicate (by key)
    seen = set()
    deduped = []
    for f in out:
        if f["key"] in seen:
            continue
        seen.add(f["key"])
        deduped.append(f)
    return deduped


# ── Dropped-item detection (Tier3) ───────────────────────────────────────────

def _collect_dropped(plugin_dir: Path, manifest: Dict[str, Any], kind: str) -> List[Dict[str, str]]:
    dropped: List[Dict[str, str]] = []

    def _drop(t: str, name: str, reason: str) -> None:
        dropped.append({"type": t, "name": name, "reason": reason})

    # hooks — this platform has no hook runtime
    if (plugin_dir / "hooks" / "hooks.json").is_file() or manifest.get("hooks"):
        _drop("hooks", "hooks", "本平台无 hook 事件运行时，无法执行")
    # commands / prompts (slash commands)
    for d, label in (("commands", "slash 命令"), ("prompts", "prompts 命令")):
        cdir = plugin_dir / d
        if cdir.is_dir():
            for f in sorted(cdir.glob("*.md")):
                _drop("command", f.stem, f"{label}：本平台无 slash 命令子系统（可后续转命令式技能）")
    # subagents
    adir = plugin_dir / "agents"
    if adir.is_dir():
        for f in sorted(adir.glob("*.md")):
            _drop("subagent", f.stem, "本平台 subagent 子系统未集成，暂不激活")
    # CC-specific UI/editor components
    for key, reason in (
        ("outputStyles", "Claude Code 输出样式，无对应"),
        ("lspServers", "LSP 集成，无对应"),
        ("monitors", "后台监视器，无对应"),
        ("channels", "消息通道，无对应"),
    ):
        if manifest.get(key) or (plugin_dir / ("output-styles" if key == "outputStyles" else key)).exists():
            _drop("component", key, reason)
    if (plugin_dir / "themes").is_dir() or (manifest.get("experimental") or {}).get("themes"):
        _drop("component", "themes", "Claude Code 主题，无对应")
    # Codex .app.json connectors
    if (plugin_dir / ".app.json").is_file() or manifest.get("apps"):
        _drop("component", "apps", "Codex/OpenAI App 连接器，生态耦合不可移植")
    return dropped


# ── Top-level entry point ────────────────────────────────────────────────────

def _slugify(value: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", (value or "").lower()).strip("-")
    return s or "plugin"


def _normalize_admin_config(manifest: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize the ``admin_config`` in the plugin manifest (admin-level provider credential declaration).

    Shape: {"mode":"any|all","group":...,"hint":...,"fields":[{key,label,secret,description}]}.
    Missing or empty fields → None (this plugin needs no admin config).
    """
    ac = manifest.get("admin_config")
    if not isinstance(ac, dict):
        return None
    raw_fields = ac.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        return None
    fields: List[Dict[str, Any]] = []
    for f in raw_fields:
        if not isinstance(f, dict) or not f.get("key"):
            continue
        fields.append({
            "key": str(f["key"]),
            "label": str(f.get("label") or f["key"]),
            "secret": bool(f.get("secret", False)),
            "description": str(f.get("description") or ""),
        })
    if not fields:
        return None
    mode = str(ac.get("mode") or "all").lower()
    if mode not in ("any", "all"):
        mode = "all"
    return {
        "mode": mode,
        "group": str(ac.get("group") or ""),
        "hint": str(ac.get("hint") or ""),
        "fields": fields,
    }


def normalize_plugin_dir(plugin_dir: Path) -> NormalizedPlugin:
    """Read any plugin directory (native/CC/Codex) into a unified NormalizedPlugin."""
    kind, manifest_path = detect_manifest(plugin_dir)
    manifest = _read_json(manifest_path)

    slug = _slugify(str(manifest.get("name") or plugin_dir.name))
    name = str(manifest.get("display_name") or manifest.get("name") or slug)
    version = str(manifest.get("version") or "1.0.0")
    description = str(manifest.get("description") or "")
    category = str(manifest.get("category") or "")
    icon = manifest.get("icon")
    if not icon and isinstance(manifest.get("interface"), dict):  # Codex
        icon = manifest["interface"].get("composerIcon") or manifest["interface"].get("logo")

    skills = _discover_skills(plugin_dir)
    mcp_map = _find_mcp_map(plugin_dir, manifest)
    mcp = [_normalize_mcp_entry(n, raw) for n, raw in sorted(mcp_map.items())]

    required_secrets = _normalize_required_secrets(manifest)
    admin_config = _normalize_admin_config(manifest)
    connection = manifest.get("connection")
    connection = str(connection).strip() if connection else None
    dropped = _collect_dropped(plugin_dir, manifest, kind)

    # default_enabled: native uses the manifest; CC/Codex default to all skills + remote MCP on, stdio MCP off
    de = manifest.get("default_enabled")
    if isinstance(de, dict):
        default_enabled = {
            "skills": [str(x) for x in (de.get("skills") or [])],
            "mcp": [str(x) for x in (de.get("mcp") or [])],
        }
    else:
        default_enabled = {
            "skills": [s.name for s in skills],
            "mcp": [m.name for m in mcp if not m.needs_runtime],
        }

    if not skills and not mcp:
        raise BadRequestError(message="插件包内未发现可导入的技能或 MCP（仅含不可移植组件？）")

    return NormalizedPlugin(
        slug=slug, name=name, version=version, description=description,
        category=category, icon=icon, kind=kind,
        required_secrets=required_secrets, default_enabled=default_enabled,
        skills=skills, mcp=mcp, dropped=dropped, admin_config=admin_config,
        connection=connection,
    )
