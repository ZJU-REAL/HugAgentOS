"""Repair active/default code prompts while retaining prior versions for rollback.

Run inside the backend container; preview by default, write only with --apply.
"""

import argparse
import copy
import json
import re
import uuid
from datetime import datetime, timezone

FACTS = (
    "已挂载空间中的 Bash `mkdir`/`mv`/`rm` 会按实际权限执行并由监听器同步。"
    "`space_delete` 同时支持个人空间和已授权团队空间；"
    "`space_move`/`space_create_folder` 当前仅支持个人空间。"
    "已有空间文件可直接移动或删除，无需先 pin；pin 只负责对话附件展示。"
)


def repair(content):
    names = {
        "Delete": "space_delete",
        "Move": "space_move",
        "CreateFolder": "space_create_folder",
        "list_myspace_files": "space_list_myspace_files",
    }
    content = re.sub(
        r"\b(Delete|Move|CreateFolder|list_myspace_files)\b", lambda m: names[m[0]], content
    )
    lines = []
    for line in content.splitlines():
        if ("bash" in line or "Bash" in line) and "不生效" in line and ("rm" in line or "mv" in line):
            continue
        if (
            "必须先" in line
            and "`pin_to_workspace`" in line
            and ("space_move" in line or "space_delete" in line)
        ):
            continue
        if line.startswith("- 存 →") and "pin_to_workspace" in line:
            line = "- 存 → 用 Write 或 Bash 写入 /myspace/；监听器自动同步。pin_to_workspace 只展示对话附件。"
        if line.startswith("- 存进某文件夹 →") and "pin_to_workspace" in line:
            line = "- 存进某文件夹 → 摸清结构 → 缺文件夹才建 → Write 写入目标路径，或 space_move 移动已有个人空间文件。"
        if line.startswith("三条铁律"):
            line = "我的空间操作规则："
        lines.append(line)
    text = "\n".join(lines).strip()
    if "已挂载空间中的 Bash" not in text:
        text += "\n\n" + FACTS
    return text


def patch_parts(parts):
    patched = copy.deepcopy(parts)
    found = False
    for part in patched:
        if (part.get("part_id") or "").split("/")[-1] == "10_tools_and_capabilities":
            part["content"] = repair(part.get("content") or "")
            found = True
    if not found:
        raise ValueError("Code capability part missing; refusing an unrelated prompt update")
    return patched


def update(apply=False):
    from core.db.engine import SessionLocal
    from core.db.models import ContentBlock
    from core.content.content_blocks import PROMPT_VERSIONS_BLOCK_ID
    from core.services.prompt_version_service import invalidate_cache
    from prompts.prompt_runtime import invalidate_prompt_cache

    with SessionLocal() as db:
        row = db.query(ContentBlock).filter_by(id=PROMPT_VERSIONS_BLOCK_ID).with_for_update().one()
        payload = copy.deepcopy(row.payload)
        versions = payload["versions"]
        old_id = payload["active"]["code_exec"]
        current = next(
            v for v in versions if v.get("kind") == "code_exec" and v.get("id") == old_id
        )
        parts = patch_parts(current["parts"])
        now = datetime.now(timezone.utc)
        suffix = now.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        new_id = old_id
        if parts != current["parts"]:
            replacement = copy.deepcopy(current)
            new_id = "space-tools-" + suffix
            replacement.update(
                id=new_id,
                name="空间文件工具规则更新",
                parts=parts,
                created_at=now.isoformat(),
                updated_at=now.isoformat(),
            )
            versions.append(replacement)
            payload["active"]["code_exec"] = new_id
        default = next(
            (v for v in versions if v.get("kind") == "code_exec" and v.get("id") == "default"), None
        )
        default_changed = False
        if default:
            default_parts = patch_parts(default["parts"])
            if default_parts != default["parts"]:
                previous = copy.deepcopy(default)
                previous["id"] = "default-before-space-tools-" + suffix
                versions.append(previous)
                default.update(parts=default_parts, updated_at=now.isoformat())
                default_changed = True
        changed = new_id != old_id or default_changed
        if apply and changed:
            row.payload, row.updated_at, row.updated_by = payload, now, "system"
            db.commit()
        else:
            db.rollback()
    if apply and changed:
        invalidate_cache()
        invalidate_prompt_cache()
    return {
        "applied": bool(apply and changed),
        "previous_active": old_id,
        "active": new_id if apply else old_id,
        "planned_active": new_id,
        "default_updated": bool(default_changed),
        "rollback_version_retained": True,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    print(json.dumps(update(parser.parse_args().apply), ensure_ascii=False))
