"""Limits and patterns used to validate generated skills."""
from __future__ import annotations
import re

MAX_ATTEMPTS = 2


MAX_BODY_CHARS = 4000


MIN_BODY_CHARS = 120


DRAFT_TIMEOUT_S = 60


CODE_FENCE = re.compile(r"```(?:\w+)?\s*\n", re.MULTILINE)


URL = re.compile(r"https?://", re.IGNORECASE)


FRONTMATTER_NAME = re.compile(r"^[a-z0-9_-]+$")


TOOL_MENTION = re.compile(r"`([A-Za-z_][A-Za-z0-9_.-]*)`")


REJECT_NO_FRONTMATTER = "missing_frontmatter"


REJECT_BAD_NAME = "invalid_skill_id"


REJECT_UNKNOWN_TOOL = "tool_not_in_evidence"


REJECT_CODE_BLOCK = "contains_code_block"


REJECT_EXTERNAL_URL = "contains_external_url"


REJECT_TOO_LONG = "body_too_long"


REJECT_TOO_SHORT = "body_too_short"


REJECT_NO_SCOPE = "missing_scope_section"


REJECT_NO_CHECKPOINTS = "missing_checkpoints"


REJECT_EMPTY = "empty_draft"


DRAFT_PROMPT = """你在为一个 AI 助手撰写一份「技能文档」(SKILL.md)。这份文档会被模型在遇到同类任务时读取并遵照执行。

# 你拿到的证据

## 用户实际提出过的请求（{objective_count} 条）
{objectives}

## 成功执行时用过的工具
{tools}

## 成功的调用顺序
{sequences}

## 失败的执行（这些是最宝贵的部分，说明坑在哪）
{failures}

## 已验证的顺序约束
{constraints}

## 这些任务里沉淀下来的做法/口径
{procedures}

# 硬性要求（违反任何一条，文档会被机器校验打回）

1. **只能引用上面「成功执行时用过的工具」里出现过的工具名**，一个都不能新增、不能猜。
2. **开头第一段必须是「## 不适用范围」**，写明本技能不适用于哪些场景。{exclusion_hint}
3. 必须有「## 适用条件」，写清楚**遇到什么样的请求才该用本技能**——要具体到能和别的任务区分开，不能写"当需要用到这些工具时"这种同义反复。
4. 必须有「## 步骤」，每一步都要写清楚：做什么、**关键参数怎么给**、**怎么判断这一步做对了**（检查点）。
5. 必须有「## 常见失败与处理」，基于上面的失败证据写。
6. **禁止**出现任何代码块（``` 包裹的内容）、禁止出现外部链接（http/https）。
7. 全文不超过 {max_chars} 字符。
8. 用中文撰写，语气是给同事写操作手册，不要写"本技能由AI生成"之类的话。

{retry_hint}

# 输出格式

严格按下面结构输出，不要加任何额外说明，不要用代码块包裹整篇文档：

DISPLAY_NAME: <一行技能名，≤20字，说明这个技能干什么>
DESCRIPTION: <一行描述，≤80字，说明什么时候该用它——模型只看这一行决定要不要打开本技能>
BODY:
## 不适用范围
...
## 适用条件
...
## 步骤
...
## 常见失败与处理
...
"""
