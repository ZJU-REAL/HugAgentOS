"""L3 graph extractor for stable declarative relationships.

The graph is deliberately complementary to the procedural L2 store. It records
who/what is connected to whom/what, while L2 records how a task should be done.
Only relationships asserted or explicitly confirmed by the user are eligible;
assistant-only claims are not evidence.
"""

from __future__ import annotations

from typing import Optional

from core.memory.extractors._base import fill_prompt, parse_json, run_llm_with_prompt
from core.memory.graph import PREDICATE_LABELS

PROMPT = """你是一个 L3 知识图谱关系抽取器。只抽取用户明确陈述或明确确认的、跨会话仍有价值的稳定实体关系。

【L3 与其它层的边界】
- L1 是用户身份和表达偏好，不在这里重复保存。
- L2 是“怎么做”的步骤、顺序、口径和红线，不在这里重复保存。
- L3 是“谁/什么 与 谁/什么 有什么关系”的声明式知识。

【允许的关系 predicate】
{predicates}

【适合写入 L3】
- 组织、团队、人员、项目之间的隶属或负责关系
- 系统、项目、文档、指标之间的依赖、使用、组成、产出关系
- 稳定的别名、分类、地理归属和领域概念关系

【绝对不写入】
- 助手单方面提出、用户没有确认的事实
- 置信度低于 0.65、指代不清或实体边界不确定的关系
- 会快速变化的数值、价格、状态、排名、新闻
- 一次性任务步骤或本轮临时要求
- 密码、Token、手机号、邮箱、证件号、内网地址等敏感信息
- 模型凭常识猜出的关系

【实体类型】
person / organization / team / project / system / document / metric / concept / place / other

【输出格式（严格 JSON，无代码块）】
{{"relations": [{{
  "source": "实体A",
  "source_type": "project",
  "predicate": "depends_on",
  "target": "实体B",
  "target_type": "system",
  "confidence": 0.9
}}]}}

没有可靠关系时输出：{{"relations": []}}

今天是 {curr_date}。

对话：
[USER] {user_msg}
[ASSISTANT] {assistant_msg}

仅返回合法 JSON。"""


async def extract(user_msg: str, assistant_msg: str, timeout_s: int) -> Optional[dict]:
    predicates = "\n".join(f"- {key}: {label}" for key, label in PREDICATE_LABELS.items())
    prompt = fill_prompt(
        PROMPT,
        user_msg,
        assistant_msg,
        extra={"predicates": predicates},
    )
    raw = await run_llm_with_prompt(prompt, timeout_s=timeout_s, max_tokens=900)
    if raw is None:
        return None
    parsed = parse_json(raw, require_key="relations")
    if not isinstance(parsed, dict) or not isinstance(parsed.get("relations"), list):
        return None
    return parsed
