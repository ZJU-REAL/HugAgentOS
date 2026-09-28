"""Rewrite component identities and package path variables before immutable publication."""
import re
import yaml
from core.agent_skills.binary_files import decode_binary, is_binary_value


def files(child, skill_ids, server_ids, directory, plugin_directory):
    def text(value):
        for token in ("${CLAUDE_SKILL_DIR}", "${CODEX_SKILL_DIR}", "${SKILL_DIR}"):
            value = value.replace(token, str(directory))
        for token in ("${PLUGIN_ROOT}", "${CLAUDE_PLUGIN_ROOT}", "${CODEX_PLUGIN_ROOT}"):
            value = value.replace(token, str(plugin_directory))
        for name in sorted(skill_ids, key=len, reverse=True):
            value = re.sub(r"\.\./" + re.escape(name) + r"(?=/|[\s\"'`)]|$)", "../" + skill_ids[name], value)
        return value
    content = text(child.skill_content)
    if content.startswith("---"):
        parts = content.split("---", 2)
        meta = yaml.safe_load(parts[1]) or {}
        for field in ("mcp-server-ids", "mcp_servers", "mcp_server_ids"):
            values = meta.get(field)
            if isinstance(values, list):
                meta[field] = [server_ids.get(v, v) for v in values]
            elif isinstance(values, str):
                meta[field] = " ".join(server_ids.get(v, v) for v in re.split(r"[,\s]+", values) if v)
        dependencies = meta.get("dependencies")
        if isinstance(dependencies, list):
            for dep in dependencies:
                if isinstance(dep, dict) and dep.get("kind") == "mcp":
                    for field in ("id", "key", "server_id"):
                        if field in dep:
                            dep[field] = server_ids.get(dep[field], dep[field])
        content = "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---" + parts[2]
    return {"SKILL.md": content, **{n: decode_binary(v) if is_binary_value(v) else text(v) for n, v in child.extra_files.items()}}
