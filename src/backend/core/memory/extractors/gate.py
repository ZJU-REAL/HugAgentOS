"""LLM write gate — one fast judgment call before any extractor runs.

The regex classify answers "which extractors *could* this turn concern"; it is
deliberately loose (identity, preference and procedural all clear it on any
substantive turn), so on its own the pipeline extracts on nearly every
substantive turn and the store fills with marginal entries. This gate adds the judgment the regex cannot make: **is there
anything in this turn worth remembering long-term at all, and of which kinds?**

Contract:
- The gate can only **narrow** the regex candidate set, never widen it. The
  regex stays the recall floor; the gate is the precision layer.
- Fail-open: if the memory LLM is down, times out, or answers garbage, the
  candidates pass through unchanged. A flaky judge must degrade to today's
  behavior (over-writing), not to silently remembering nothing.
- One call, small answer. The whole point is that a ~50-token verdict is far
  cheaper than the up-to-four extractor calls it saves on an empty turn.
"""

from __future__ import annotations

import logging

from core.memory.extractors._base import fill_prompt, parse_json, run_llm_with_prompt
from core.memory.extractors.router import ExtractorType

logger = logging.getLogger(__name__)

# The gate reads a prefix of each side, not the full transcript: its question is
# "does this turn contain memory-worthy signal", which the opening of each
# message answers; the extractors that follow read the full text anyway.
_GATE_INPUT_CHARS = 2000

PROMPT = """你是一个记忆写入门卫。判断这轮对话中是否包含**值得长期记住**的信息，以及属于哪些类别。

【类别定义】
- identity: 用户的身份信息（姓名、部门、岗位、单位、联系方式）
- preference: 稳定的表达偏好（格式、详略、语言风格、明确禁忌）
- procedural: 可复用的做事方式（步骤顺序、口径定义、校验红线、交付约定）
- graph: 稳定的实体关系（隶属、负责、依赖、使用、组成、别名、分类）
- task: 本轮明确提出的多步任务目标（仅本会话内使用）

【判定标准】
以下内容都不算：
- 一次性的问答、查询、闲聊
- 只对当前这一次请求有效的指令（"这次用表格""先看第三页"）
- 助手自己说的内容（除非用户明确认可为约定）
- 模型本来就会的通用常识

以下内容应当记住：
- 用户明确要求“记住”“记录一下”“以后”“下次”“默认”“一律”“不要再”，并给出需要延续的信息；明确说明仅本次有效的除外
- 用户直接陈述或确认的身份、岗位、部门、单位、职责、联系方式等相对稳定的信息 → identity
- 用户反复适用的表达与交付偏好，例如语言、详略、格式、称呼、禁忌和默认输出方式 → preference
- 用户所在组织、团队或项目特有的可复用规则，例如术语定义、统计口径、计算方法、步骤顺序、命名规范、目录约定、必做校验、交付标准和风险红线 → procedural
- 用户直接陈述或确认的稳定实体关系，例如谁负责什么、哪个部门隶属哪里、系统依赖什么、产品由哪些模块组成、名称与别名的对应关系 → graph
- 用户明确提出的多步任务，以及尚未完成的目标、步骤、进度、截止时间和交付物 → task；此类信息只在本会话内保留，不作为长期事实
- 用户对已有信息的纠正、替换或失效声明，例如“不是 A，是 B”“我现在改负责 X”“以后不用旧口径”；应记住新信息并用于更新旧记忆
- 助手提出的方案被用户明确确认为今后的规则或偏好，例如“对，以后都按这个执行”；按对应类别记住

总判断原则：
- 只有用户明确说出或明确确认的内容才能作为事实写入，不要把助手的推测当成用户记忆
- 判断它在未来是否仍然有用：如果下次对话不知道这条信息会导致做错、重复询问或违反用户约定，就应该记住
- 用户明确要求记住时应优先保留；若内容同时符合多个类别，可以返回多个类别
- 不要因为信息只出现一次就拒绝；稳定性由语义和用户意图判断，而不是由出现次数判断

【输出格式（严格 JSON，无代码块包裹）】
{{"classes": ["identity", "procedural"]}}

没有任何值得记的内容时输出：
{{"classes": []}}

对话：
[USER] {user_msg}
[ASSISTANT] {assistant_msg}

仅返回合法 JSON。"""


async def llm_write_gate(
    user_msg: str,
    assistant_msg: str,
    candidates: set[ExtractorType],
    timeout_s: int,
) -> set[ExtractorType]:
    """Return the subset of ``candidates`` the LLM judges worth extracting.

    On any failure the candidates come back unchanged (fail-open) — see module
    docstring for why that is the right degradation direction.
    """
    if not candidates:
        return candidates

    prompt = fill_prompt(
        PROMPT,
        (user_msg or "")[:_GATE_INPUT_CHARS],
        (assistant_msg or "")[:_GATE_INPUT_CHARS],
    )
    raw = await run_llm_with_prompt(prompt, timeout_s=timeout_s, max_tokens=150)
    if raw is None:
        logger.info(
            "[memory_gate] LLM unavailable, failing open (%d candidates pass)", len(candidates)
        )
        return candidates

    parsed = parse_json(raw, require_key="classes")
    if not isinstance(parsed, dict) or not isinstance(parsed.get("classes"), list):
        logger.info("[memory_gate] unparseable verdict, failing open: %r", raw[:120])
        return candidates

    approved: set[ExtractorType] = set()
    for name in parsed["classes"]:
        try:
            approved.add(ExtractorType(str(name).strip().lower()))
        except ValueError:
            continue

    verdict = candidates & approved
    if verdict != candidates:
        dropped = sorted(c.value for c in candidates - verdict)
        logger.info(
            "[memory_gate] narrowed %s → %s (dropped %s)",
            sorted(c.value for c in candidates),
            sorted(c.value for c in verdict),
            dropped,
        )
    return verdict
