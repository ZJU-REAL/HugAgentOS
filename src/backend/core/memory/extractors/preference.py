"""PREFERENCE extractor —— writes into the L1 Profile.

Only extracts stable, reusable expression preferences that are meaningful guidance for future conversations.
One-off requests ("just use a table this time") are not extracted.
"""

from __future__ import annotations

from typing import Optional

from core.memory.extractors._base import fill_prompt, parse_json, run_llm_with_prompt

PROMPT = """你是一个用户偏好抽取器。只抽取**稳定、可复用**的表达偏好。

【必须抽取】
- 输出格式偏好（表格 / 列表 / 段落 / 图表 / 代码块）
- 详略偏好（简洁 / 完整分析）
- 语言风格（正式 / 口语 / 公文 / 英文）
- 明确禁忌（"不要用 emoji""不要列 citation""不要直接改数据库"）
- 工具使用倾向（"先让我确认再执行""能并行就并行"）

【绝对不抽取】
- 一次性请求（"这次就用表格""这题不用解释"）
- 对某条具体数据的单次反应
- 涉及具体业务内容的倾向（属于 FACT 或 IDENTITY）

【输出格式（严格 JSON，无代码块包裹）】
{{"facts": [{{"field": "output_format|verbosity|style|prohibited|tool_behavior", "value": "...", "strength": "strong|weak"}}]}}

【示例】
user: 以后回答简短点，表格能说清就别写段落
output: {{"facts": [
  {{"field": "verbosity", "value": "简洁", "strength": "strong"}},
  {{"field": "output_format", "value": "表格优先", "strength": "strong"}}
]}}

user: 这次用文字说吧
output: {{"facts": []}}

今天是 {curr_date}。

对话：
[USER] {user_msg}
[ASSISTANT] {assistant_msg}

仅返回合法 JSON。"""


async def extract(user_msg: str, assistant_msg: str, timeout_s: int) -> Optional[dict]:
    prompt = fill_prompt(PROMPT, user_msg, assistant_msg)
    raw = await run_llm_with_prompt(prompt, timeout_s=timeout_s, max_tokens=500)
    if raw is None:
        return None
    parsed = parse_json(raw)
    if not isinstance(parsed, dict) or "facts" not in parsed:
        return None
    return parsed
