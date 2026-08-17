#!/usr/bin/env python3
"""打包前自检一个插件目录的结构是否合法（导入前跑一遍，省得来回试错）。

规则对齐后端导入器 core/services/plugin_importer.py：
- 包根必须有 plugin.json（或 .claude-plugin/plugin.json）；
- plugin.json 必须是合法 JSON 且含非空 name；
- name 必须匹配 ^[a-z0-9_-]{1,100}$（会被用来生成安装 id）；
- skills/ 下每个子目录都必须有 SKILL.md，且其 frontmatter 含 name + description；
- mcp.json（若有）必须是合法 JSON 且含 mcpServers 对象；
- extensions["org.hugagent"].mcp 里的服务名必须能在 mcp.json 里找到（否则展示元数据贴不上）；
- 包内不得有目录穿越式路径或指向包外的软链接。

纯标准库，可直接在沙箱里跑：
    python3 validate_plugin.py <插件目录>
退出码 0=通过，1=不通过。警告不影响退出码。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

_SLUG_RE = re.compile(r"^[a-z0-9_-]{1,100}$")
_SKILL_ID_RE = re.compile(r"^[a-z0-9_-]{1,63}$")

errors: List[str] = []
warnings: List[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def warn(msg: str) -> None:
    warnings.append(msg)


def _read_json(path: Path) -> Tuple[Dict[str, Any], bool]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        err(f"{path.name} 不是合法 JSON：{exc}")
        return {}, False
    if not isinstance(data, dict):
        err(f"{path.name} 顶层必须是一个对象。")
        return {}, False
    return data, True


def _split_frontmatter(text: str) -> Dict[str, str]:
    """极简 frontmatter 解析：只取 `key: value` 单行字段，够本脚本判空用。"""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out: Dict[str, str] = {}
    for line in text[3:end].splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


def check_manifest(root: Path) -> Dict[str, Any]:
    native = root / "plugin.json"
    nested = root / ".claude-plugin" / "plugin.json"
    if native.is_file():
        mp = native
    elif nested.is_file():
        mp = nested
        warn("清单在 .claude-plugin/ 下（兼容布局）。新包建议直接放包根的 plugin.json。")
    else:
        err("包根没有 plugin.json —— 这不是一个插件包，import_plugin 会拒绝。")
        return {}

    manifest, ok = _read_json(mp)
    if not ok:
        return {}

    name = str(manifest.get("name") or "").strip()
    if not name:
        err("plugin.json 缺 name（它就是 slug，安装 id 由它生成）。")
    elif not _SLUG_RE.match(name):
        err(f"plugin.json 的 name「{name}」不合法：只能用小写字母、数字、下划线、连字符，1–100 字符。")

    if not str(manifest.get("description") or "").strip():
        warn("plugin.json 没写 description —— 用户在插件市场里看不到这个插件是干什么的。")
    if not str(manifest.get("version") or "").strip():
        warn("plugin.json 没写 version，导入时会按 1.0.0 处理。")
    return manifest


def check_skills(root: Path) -> int:
    skills_dir = root / "skills"
    if not skills_dir.is_dir():
        return 0
    count = 0
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir():
            continue
        sm = child / "SKILL.md"
        if not sm.is_file():
            err(f"skills/{child.name}/ 里没有 SKILL.md —— 这个目录会被导入器直接忽略。")
            continue
        count += 1
        fm = _split_frontmatter(sm.read_text(encoding="utf-8", errors="replace"))
        if not fm:
            err(f"skills/{child.name}/SKILL.md 没有 --- 包裹的 YAML frontmatter。")
            continue
        sname = fm.get("name", "")
        if not sname:
            err(f"skills/{child.name}/SKILL.md 的 frontmatter 缺 name。")
        elif not _SKILL_ID_RE.match(sname):
            err(
                f"skills/{child.name}/SKILL.md 的 name「{sname}」不合法："
                "只能用小写字母、数字、下划线、连字符，1–63 字符。"
            )
        if not fm.get("description"):
            err(
                f"skills/{child.name}/SKILL.md 的 frontmatter 缺 description —— "
                "它是技能被唤起的主要依据，不能空。"
            )
    return count


def check_mcp(root: Path, manifest: Dict[str, Any]) -> int:
    mcp_path = root / "mcp.json"
    servers: Dict[str, Any] = {}
    if mcp_path.is_file():
        data, ok = _read_json(mcp_path)
        if ok:
            servers = data.get("mcpServers") or {}
            if not isinstance(servers, dict):
                err("mcp.json 的 mcpServers 必须是一个对象。")
                servers = {}
            elif not servers:
                warn("mcp.json 里 mcpServers 是空的 —— 那就不必带这个文件。")
            for sname, cfg in servers.items():
                if not isinstance(cfg, dict):
                    err(f"mcp.json 的服务「{sname}」配置必须是对象。")
                    continue
                if not (cfg.get("url") or cfg.get("command")):
                    err(f"mcp.json 的服务「{sname}」既没有 url 也没有 command，无法连接。")

    # 扩展段里的展示元数据必须能对上 mcp.json 里的服务名，否则贴不上去（静默失效）。
    ext = ((manifest.get("extensions") or {}).get("org.hugagent") or {})
    ext_mcp = ext.get("mcp") if isinstance(ext.get("mcp"), dict) else {}
    for sname in ext_mcp:
        if sname not in servers:
            err(
                f"extensions[org.hugagent].mcp 里写了服务「{sname}」，但 mcp.json 里没有同名服务 —— "
                "展示名与工具描述会贴不上去。两边的服务名必须完全一致。"
            )
    if servers and not ext_mcp:
        warn(
            "带了 MCP 服务但没在 extensions[org.hugagent].mcp 里写 display_name / tools 描述 —— "
            "模型会缺少判断何时该调这些工具的依据。"
        )
    return len(servers)


def check_paths(root: Path) -> None:
    base = root.resolve()
    for p in root.rglob("*"):
        if p.is_symlink():
            try:
                target = p.resolve()
            except OSError:
                err(f"软链接无法解析：{p.relative_to(root)}")
                continue
            try:
                target.relative_to(base)
            except ValueError:
                err(f"软链接指向包外，导入会被拒：{p.relative_to(root)} → {target}")
        # 不必再查 ".." / NUL：rglob 枚举的是真实存在的目录项，永远不会产出 ".." 这样的
        # 路径分量，文件名里也不可能有 NUL。真正的穿越风险在归档条目里（打包之后），
        # 由后端解包时的 within() 拦截；这里能查的只有上面那条软链接越界。


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1
    root = Path(sys.argv[1]).expanduser()
    if not root.is_dir():
        print(f"❌ 不是一个目录：{root}")
        return 1

    manifest = check_manifest(root)
    n_skills = check_skills(root)
    n_mcp = check_mcp(root, manifest) if manifest else 0
    check_paths(root)

    if manifest and n_skills == 0 and n_mcp == 0:
        err("这个包既没有技能也没有 MCP 服务 —— 装了等于什么都没加。")

    for w in warnings:
        print(f"⚠️  {w}")
    for e in errors:
        print(f"❌ {e}")

    if errors:
        print(f"\n不通过：{len(errors)} 个问题需要修复。")
        return 1
    print(f"\n✅ 通过：{n_skills} 个技能、{n_mcp} 个 MCP 服务。可以打包了：")
    print(f"   tar -czf /workspace/plugin.tgz -C {root} .")
    return 0


if __name__ == "__main__":
    sys.exit(main())
