"""Shared skill installation identity, content and credentials transformations."""
from __future__ import annotations
import hashlib
import json
import re
from typing import Any, Dict, List, Optional
from core.capabilities.skills.registry import split_frontmatter
from core.infra.exceptions import BadRequestError

SECRETS_FILENAME = "secrets.json"

def strip_frontmatter(raw: str) -> str:
    try:
        _, body = split_frontmatter(raw)
    except Exception:  # noqa: BLE001
        body = raw
    return (body or "").strip()


def owner_suffix(owner_user_id: str) -> str:
    """Stable 6-char fingerprint derived from user_id, used to deduplicate private-install skill ids."""
    return hashlib.sha1(owner_user_id.encode("utf-8")).hexdigest()[:6]


def compute_install_id(entry_name: str, owner_user_id: Optional[str]) -> str:
    """The installed AdminSkill.skill_id: admin = original name (global); user = original name + fingerprint (private)."""
    if owner_user_id is None:
        return entry_name
    return f"{entry_name}-{owner_suffix(owner_user_id)}"


def base_entry_name(skill_id: str, owner_user_id: Optional[str]) -> str:
    """Inverse of ``compute_install_id``: recover the underlying market entry_name from an install id.

    Private install ids look like ``{entry_name}-{fingerprint}``; stripping that
    user's 6-char fingerprint suffix yields the entry_name. Global ids (owner is
    None) already are the entry_name and are returned as-is. Used to merge the
    "privately installed version" and the "admin global version" onto the same
    underlying skill for deduplication — preventing the same skill from
    appearing twice in the skill library / at runtime.
    """
    if owner_user_id is None:
        return skill_id
    suffix = f"-{owner_suffix(owner_user_id)}"
    if skill_id.endswith(suffix):
        return skill_id[: -len(suffix)]
    return skill_id


def rewrite_frontmatter_name(content: str, new_name: str) -> str:
    """Rewrite the ``name:`` in the SKILL.md frontmatter to ``new_name`` (touching only that line).

    Replaces only the first ``name:`` line within the frontmatter block; all
    other content (including multi-line descriptions) is preserved verbatim.
    Returned unchanged when there is no frontmatter.
    """
    if not content.startswith("---"):
        return content
    end = content.find("\n---", 3)
    if end == -1:
        return content
    head = content[: end + 1]
    tail = content[end + 1 :]
    head = re.sub(r"(?m)^name:[ \t]*.*$", f"name: {new_name}", head, count=1)
    return head + tail


def inject_secrets(
    skill_content: str,
    extra_files: Dict[str, str],
    required_secrets: List[Dict[str, Any]],
    secrets: Dict[str, str],
) -> str:
    """Write the user-provided credentials into secrets.json and append a "credentials configuration" section at the end of SKILL.md.

    Returns the SKILL.md content with the section appended. Missing required
    fields raise a 400.
    """
    cleaned: Dict[str, str] = {}
    for field in required_secrets:
        key = str(field.get("key") or "").strip()
        if not key:
            continue
        val = (secrets.get(key) or "").strip()
        if not val:
            if field.get("required"):
                label = field.get("label") or key
                raise BadRequestError(message=f"请填写必填凭据：{label}")
            continue
        cleaned[key] = val
    if not cleaned:
        return skill_content

    extra_files[SECRETS_FILENAME] = json.dumps(cleaned, ensure_ascii=False, indent=2)
    lines = [
        "",
        "---",
        "",
        "## 🔑 凭据配置（由安装者提供）",
        "",
        f"运行本技能所需的凭据已写入技能目录下的 `{SECRETS_FILENAME}`，包含以下字段：",
        "",
    ]
    for key in cleaned:
        lines.append(f"- `{key}`")
    lines += [
        "",
        f"执行技能脚本前，请先从 `{SECRETS_FILENAME}` 读取这些值，并以**同名环境变量**导出后再运行，例如：",
        "",
        "```bash",
        f'export {next(iter(cleaned))}="$(python3 -c "import json;print(json.load(open(\'{SECRETS_FILENAME}\'))[\'{next(iter(cleaned))}\'])")"',
        "```",
        "",
        "> 凭据仅存于本技能目录，请勿在对话中明文回显。",
        "",
    ]
    return skill_content.rstrip() + "\n" + "\n".join(lines)
