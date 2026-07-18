"""TASK extractor -- writes to the Session auxiliary layer (`chats.metadata.session_memory`).

Only extracts the current session's task goal / progress / to-dos; promoted or discarded after the session ends.
"""

from __future__ import annotations

from typing import Optional

from core.memory.extractors._base import fill_prompt, parse_json, run_llm_with_prompt

PROMPT = """你是一个会话任务工作集抽取器。跟踪本次会话的目标 / 进度 / 待办。

【必须抽取】
- 本会话的核心目标（1 句话）
- 已完成的步骤（列表）
- 待办的步骤（列表）

【绝对不抽取】
- 跨会话的长期目标（那是 FACT 层）
- 用户身份 / 偏好
- 具体工具调用日志细节

【输出格式（严格 JSON，无代码块包裹）】
{{"session_task": {{
    "goal": "...",
    "done": ["...", "..."],
    "pending": ["...", "..."]
}}}}

若本轮明显不是任务型对话（如闲聊、致谢），返回 {{"session_task": null}}。

【示例】
user: 先查 Q3 数据，再生成环比表，最后写 200 字总结
assistant: 已查到 Q3 数据
output: {{"session_task": {{
  "goal": "Q3 数据环比分析与总结",
  "done": ["查询 Q3 原始数据"],
  "pending": ["生成环比分析表", "写 200 字总结"]
}}}}

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
    if not isinstance(parsed, dict):
        return None
    return parsed
