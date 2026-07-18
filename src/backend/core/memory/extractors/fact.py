"""FACT extractor -- writes to L2 Milvus factual memory.

Only extracts business facts that have reuse value for future queries.
"""

from __future__ import annotations

from typing import Optional

from core.memory.extractors._base import fill_prompt, parse_json, run_llm_with_prompt

PROMPT = """你是一个业务事实抽取器。只抽取**对未来查询有复用价值**的事实。

【必须抽取】
- 用户查询过并由助手给出的具体数据（必须含来源）
- 用户提到并可能反复引用的业务实体 / 项目 / 产品 / 报告
- 用户口头确认的业务口径定义（"我们口径的 X 包含 Y"）
- 用户与助手达成的方法论结论（"A 方案比 B 好，因为…"）

【绝对不抽取】
- 本次会话临时任务（属于 TASK 层）
- 用户身份 / 偏好（属于 IDENTITY / PREFERENCE）
- 助手未明确引用来源的数据（防止写入幻觉）
- 助手回复中带"可能不准确""不确定""无法核实"等警示语的内容
- 红头文件号明文（保留文件主题，编号必须为 REDACTED）
- 任何带"机密 / 秘密 / 内部"字样的内容

【输出格式（严格 JSON，无代码块包裹）】
{{"facts": [{{
    "content": "...",           // 一句话事实，≤200 字
    "source": "conversation|document|tool:<name>",
    "tags": ["..."],            // ≤3 个业务标签
    "confidentiality": "public|internal|sensitive",
    "ttl_days": 180,
    "evidence": "短引用原文（≤60 字）"
}}]}}

【示例】
user: 查一下 Q3 营收
assistant: 根据 Q3 财报，营收 32.1 亿元，同比 +12%
output: {{"facts": [{{
  "content": "2025 Q3 营收 32.1 亿元，同比 +12%",
  "source": "conversation",
  "tags": ["营收", "2025Q3"],
  "confidentiality": "internal",
  "ttl_days": 365,
  "evidence": "根据 Q3 财报，营收 32.1 亿元"
}}]}}

user: 这个数据对不对
output: {{"facts": []}}

今天是 {curr_date}。

对话：
[USER] {user_msg}
[ASSISTANT] {assistant_msg}

仅返回合法 JSON。"""


async def extract(user_msg: str, assistant_msg: str, timeout_s: int) -> Optional[dict]:
    prompt = fill_prompt(PROMPT, user_msg, assistant_msg)
    raw = await run_llm_with_prompt(prompt, timeout_s=timeout_s, max_tokens=1200)
    if raw is None:
        return None
    parsed = parse_json(raw)
    if not isinstance(parsed, dict) or "facts" not in parsed:
        return None
    return parsed
