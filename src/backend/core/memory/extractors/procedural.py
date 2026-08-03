"""PROCEDURAL extractor — the memory type a skill can actually be compiled from.

The other extractors record what is *true* (identity, facts) or what the user
*likes* (preferences). Neither can become a skill: compiling a fact into a skill
freezes something whose whole value is that it stays current, and a preference
belongs in the profile where a single line covers every task.

What can become a skill is **how work gets done here** — the conventions,
orderings and definitions that are not derivable from the task and not present
in any model's pretraining:

    "做财务风险分析前先核验主体，同名公司会串"
    "周报口径是自然周，不是滚动 7 天"
    "配色改动要先出一版给对方确认再批量应用"

That is the knowledge the system currently re-derives on every occurrence, and
it is the only knowledge for which "compile it once into a skill" is the right
answer.

Two rules the prompt enforces, both learned from what goes wrong without them:

* **A procedure is not a one-off instruction.** "这次先做第三页" is a request;
  "改 PPT 一律先出提纲" is a procedure. Extracting the former fills the store with
  instructions that were already obeyed and will never apply again.
* **A procedure states the *why* where the user gave one.** The reason is what
  lets a reviewer — and later a skill — tell where the rule stops applying. A
  rule without a reason can only be applied everywhere or nowhere, which is
  exactly the failure measured when scope was left to prose.
"""

from __future__ import annotations

from typing import Optional

from core.memory.extractors._base import fill_prompt, parse_json, run_llm_with_prompt

MEMORY_TYPE = "procedural"

PROMPT = """你是一个"做法/口径"抽取器。只抽取**可复用的做事方式**，不抽取事实、不抽取偏好、不抽取一次性指令。

【必须抽取】
- 步骤与顺序约定（"先核验主体再取数""先出提纲再配色"）
- 口径与定义（"周报按自然周""营收含税""财年从 4 月起"）
- 校验与红线（"数据要交叉验证两个来源""不确定就标注不确定，不要猜"）
- 交付约定（"结论放最前面""每个数字要给出处"）

【绝对不抽取】
- 客观事实（"宁德时代是电池厂商"）→ 属于 FACT
- 表达偏好（"回答简短点""用表格"）→ 属于 PREFERENCE
- 一次性指令（"这次先看第三页""今天不用给出处"）
- 通用常识（"分析要客观"这类任何场景都成立、模型本来就会的内容）

【判定标准】
抽出来的每一条都要能通过这个测试：**下次遇到同类任务，不知道这条会做错吗？**
答"不会做错"的，就不要抽。

【输出格式（严格 JSON，无代码块包裹）】
{{"procedures": [{{"rule": "...", "why": "...", "applies_to": "...", "strength": "strong|weak"}}]}}

- rule: 一句话说明"该怎么做"，祈使句
- why: 用户给出的理由；没给就填 ""
- applies_to: 这条在什么任务上成立（如"财务风险分析"）；不确定就填 ""

【示例】
user: 做财务分析前一定要先核验公司主体，之前有同名公司串了数据
output: {{"procedures": [{{"rule": "做财务分析前先核验公司主体是否唯一", "why": "存在同名公司，容易串数据", "applies_to": "财务分析", "strength": "strong"}}]}}

user: 这次周报你先写第三部分吧
output: {{"procedures": []}}

user: 我们的周报口径是自然周，周一到周日
output: {{"procedures": [{{"rule": "周报统计口径按自然周（周一至周日）", "why": "", "applies_to": "周报", "strength": "strong"}}]}}

今天是 {curr_date}。

对话：
[USER] {user_msg}
[ASSISTANT] {assistant_msg}

仅返回合法 JSON。"""


async def extract(user_msg: str, assistant_msg: str, timeout_s: int) -> Optional[dict]:
    prompt = fill_prompt(PROMPT, user_msg, assistant_msg)
    raw = await run_llm_with_prompt(prompt, timeout_s=timeout_s, max_tokens=600)
    if raw is None:
        return None
    parsed = parse_json(raw, require_key="procedures")
    if not isinstance(parsed, dict) or "procedures" not in parsed:
        return None
    return parsed
