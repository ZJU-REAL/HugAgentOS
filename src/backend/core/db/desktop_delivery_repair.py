"""Upgrade only stock desktop file-delivery instructions in saved versions."""

from copy import deepcopy
import sqlalchemy as sa

REPLACEMENTS = {
    "guidance": (
        "项目文件使用 pin_to_workspace(file_paths=[...]) 展示原文件引用，不复制、不上传；临时工作目录的产物使用 sandbox_get_artifact 导出后展示。",
        "项目文件与当前会话工作目录的产物统一使用 pin_to_workspace(file_paths=[...]) 直接交付，建议传真实绝对路径。项目文件展示原文件引用；会话产物自动登记后展示，无需额外导出步骤。",
    ),
    "bash_tool": (
        "本机文件不会通过 /myspace 自动同步；需要下载链接时使用 sandbox_get_artifact。",
        "本机文件不会通过 /myspace 自动同步；交付文件时直接使用 pin_to_workspace(file_paths=[真实绝对路径])。",
    ),
}


def upgrade_desktop_delivery_prompts(conn):
    return _upgrade_parts(conn, REPLACEMENTS)


CWD_REPLACEMENTS = {
    "guidance": (
        "project_root 是绑定的项目目录，不代表工具已经切换 cwd。操作项目文件使用真实绝对路径；执行项目命令时在同一次 Bash 调用中先 cd 到带引号的项目路径。",
        "绑定本地项目时 cwd 与 project_root 相同；bash、文件工具及 pin 的相对路径均以该项目目录为基准。未绑定项目时 cwd 为当前会话的持久工作目录。",
    ),
    "project": (
        "项目文件使用真实绝对路径操作，不需要上传到「我的空间」。",
        "该目录也是 bash 的默认 cwd，以及文件工具和 pin 的相对路径基准。项目文件直接保存在这里，不需要上传到「我的空间」。",
    ),
}


def upgrade_desktop_cwd_prompts(conn):
    return _upgrade_parts(conn, CWD_REPLACEMENTS)


def _upgrade_parts(conn, replacements):
    blocks = sa.table("content_blocks", sa.column("id", sa.String), sa.column("payload", sa.JSON))
    payload = conn.execute(
        sa.select(blocks.c.payload).where(blocks.c.id == "prompt_versions")
    ).scalar_one_or_none()
    if not isinstance(payload, dict):
        return 0
    updated = deepcopy(payload)
    count = 0
    for version in updated.get("versions", []):
        if version.get("kind") != "desktop":
            continue
        for part in version.get("parts", []):
            pair = replacements.get(part.get("part_id"))
            content = part.get("content", "")
            if pair and pair[0] in content:
                part["content"] = content.replace(*pair)
                count += 1
    if count:
        conn.execute(
            blocks.update().where(blocks.c.id == "prompt_versions").values(payload=updated)
        )
    return count
