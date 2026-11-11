"""Constants for Personal Agent."""

from pathlib import Path

WORKSPACE = "/workspace"

TASK_UID = 1000

DEFAULT_NAME = "Hugo"

REQUIRED = ("AGENTS.md", "SOUL.md", "IDENTITY.md")


CONTEXT_FILES = ("AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md", "BOOTSTRAP.md", "MEMORY.md")

ROLES = {
    "AGENTS.md": "运行手册：工作区约定、记忆规则、红线",
    "SOUL.md": "人格：性格、语气、边界",
    "IDENTITY.md": "身份：名字、形象、风格、签名表情",
    "USER.md": "用户画像：用户明确说过的稳定偏好与长期指令",
    "BOOTSTRAP.md": "首次运行仪式：完成后由智能体删除",
    "MEMORY.md": "置顶记忆：## 置顶 段只收用户明确要求记住的话，每轮注入",
    "HEARTBEAT.md": "心跳清单：定期检查的事项",
    "DREAMS.md": "Dreaming 整理报告：观察、更新、去重和冲突",
    "memory": "记忆 wiki：按主题组织的页面 memory/<type>-<name>.md",
    "memory/_index.md": "记忆目录：Where to start 与分组，开头每轮注入",
    "memory/_schema.md": "记忆写作规则：平台写入，不能修改",
    "journal": "每日记录：平台自动写的 journal/YYYY-MM-DD.md 流水账，只读",
    "work": "任务目录：每个任务一个子目录",
    "outputs": "产出目录",
    "scratch": "临时目录",
}

FILE_MAX_CHARS = 20000

USER_MAX_CHARS = 4000

TOTAL_MAX_CHARS = 60000

EDIT_MAX_BYTES = 256 * 1024

READ_MAX_BYTES = 1024 * 1024

TREE_MAX_ENTRIES = 2000

TREE_MAX_DEPTH = 8

PROMPT_KIND = "personal_agent"

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

TEMPLATE_PARTS = {
    "AGENTS.md": "template_agents",
    "SOUL.md": "template_soul",
    "IDENTITY.md": "template_identity",
    "USER.md": "template_user",
    "MEMORY.md": "template_memory",
    "BOOTSTRAP.md": "template_bootstrap",
    "HEARTBEAT.md": "template_heartbeat",
}

HEARTBEAT_FILE = "HEARTBEAT.md"

BOOTSTRAP_FILE = "BOOTSTRAP.md"

PROFILE_FILES = ("SOUL.md", "IDENTITY.md", "USER.md")
