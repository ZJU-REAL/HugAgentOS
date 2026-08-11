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
* **A verified correction is multi-turn evidence.** When an assistant first
  fails, the user changes the method, and the retry succeeds, the reusable
  recovery rule is grounded in the trajectory even if the assistant is the one
  that articulates the final root-cause explanation.
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
- 经验证的失败恢复经验：近期轨迹明确显示“助手先失败/误判 → 用户给出不同做法 → 按新做法成功”。抽取正确的工具/步骤、必要校验和避免旧错误的方法；把失败原因写入 why

【绝对不抽取】
- 客观事实（"宁德时代是电池厂商"）→ 属于 FACT
- 表达偏好（"回答简短点""用表格"）→ 属于 PREFERENCE
- 一次性指令（"这次先看第三页""今天不用给出处"）
- 通用常识（"分析要客观"这类任何场景都成立、模型本来就会的内容）
- 助手单方面声称的“教训”或“正确做法”，但轨迹中没有用户纠正和成功结果

【多轮纠错的特殊规则】
- 只有同时存在先前失败、用户提供的方法变化、后续成功证据时，才按经验证纠错抽取
- 这种情况下，助手复盘中明确写出的根因和正确步骤可以作为 procedure 的证据，不视为无依据的助手自述
- strength 填 strong；applies_to 写清任务边界，避免把某个工具的经验泛化到所有任务
- 同一条失败恢复因果链优先合并为一条端到端规则，不要把工具选择、验证步骤和避免误判拆成多条近义记忆；只有彼此独立、适用场景不同的规则才拆开，最多两条

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

近期轨迹（可能与当前轮重叠）：
{recent_trajectory}

仅返回合法 JSON。"""


CORRECTION_PROMPT = """你是一个失败恢复经验抽取器。下面的轨迹已经由程序确认包含：助手先失败或误判、用户给出不同做法、随后执行成功。

请只抽取这次**已经由成功结果验证**的可复用规则：正确工具或步骤、必要校验、应避免的旧做法，以及适用任务边界。不要抽取业务事实、一次性交付内容或未经验证的猜测。

必须把同一次纠错合并成**恰好一条**端到端规则，在这一条 rule 中写全正确工具/步骤、必要校验和避免旧误判的方法；不得拆成多条。why 只写轨迹明确支持的原因，没有就留空。

【输出格式（严格 JSON，无代码块包裹）】
{{"procedures": [{{"rule": "...", "why": "...", "applies_to": "...", "strength": "strong"}}]}}

近期轨迹：
{recent_trajectory}

当前轮：
[USER] {user_msg}
[ASSISTANT] {assistant_msg}

仅返回合法 JSON。"""


async def extract(
    user_msg: str,
    assistant_msg: str,
    timeout_s: int,
    *,
    recent_trajectory: str = "",
    verified_correction: bool = False,
) -> Optional[dict]:
    prompt = fill_prompt(
        PROMPT,
        user_msg,
        assistant_msg,
        extra={"recent_trajectory": recent_trajectory},
    )
    raw = await run_llm_with_prompt(prompt, timeout_s=timeout_s, max_tokens=600)
    parsed = parse_json(raw, require_key="procedures") if raw is not None else None
    procedures = parsed.get("procedures") if isinstance(parsed, dict) else None
    if (
        isinstance(procedures, list)
        and procedures
        and (not verified_correction or len(procedures) <= 2)
    ):
        return parsed

    # Some memory models still apply the normal "one-off instruction" rule to
    # a multi-turn correction and return an empty list.  Retry only for the
    # rare deterministic signal, with a focused prompt; ordinary empty turns
    # never pay for a second model call.
    if verified_correction:
        correction_prompt = fill_prompt(
            CORRECTION_PROMPT,
            user_msg,
            assistant_msg,
            extra={"recent_trajectory": recent_trajectory},
        )
        correction_raw = await run_llm_with_prompt(
            correction_prompt,
            timeout_s=timeout_s,
            max_tokens=600,
        )
        correction = (
            parse_json(correction_raw, require_key="procedures")
            if correction_raw is not None
            else None
        )
        if isinstance(correction, dict) and isinstance(correction.get("procedures"), list):
            # The focused prompt requires exactly one consolidated procedure.
            # Enforce that output contract even when a model ignores the count.
            correction["procedures"] = correction["procedures"][:1]
            return correction

    if isinstance(parsed, dict) and "procedures" in parsed:
        return parsed
    return None
