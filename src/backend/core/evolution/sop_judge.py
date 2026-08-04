"""LLM adjudication of sequence→skill proposals — the semantic gate after the rules.

规则门（`promotion.promote_tool_sequence_to_skill` 的四道门）能测出**统计**层面
的问题：复现不足、单会话刷量、意图不聚、成功率低。但有一类问题规则写不出来，
只有读懂请求文本和工具语义才能判断：

* 意图簇可能是**巧合归并**——嵌入相似不等于同一类任务；
* 一个满足全部统计条件的序列可能**不携带区分性知识**——"搜索→整理→写文件"
  对几乎任何任务都成立，沉淀成 SOP 后对下一次执行没有任何指导价值；
* 把这套做法固化下来照做，可能有规则看不见的**明显风险**。

所以在规则幸存者上加一道 LLM 裁决。边界与技能编译的「两段式生成」一致
（见 :mod:`core.evolution.skill_gen`）：模型只回答「这值不值得沉淀为 SOP」
并给出理由和触发条件，它不能扩权、不能改工具序列、不能凭空造资产——那些
仍由代码与 IR 不变量把守。裁决结论随候选落库，评审员能看到机器放行的理由。

失败语义与全链路一致：**不确定不等于通过**。模型不可用、输出解析不了，
都按拒绝处理——但两种状态在结果里可区分（``judge_unavailable``），运维
才能把"模型挂了"和"提案被否了"分开看。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Sequence

logger = logging.getLogger(__name__)

JUDGE_TIMEOUT_S = 45
MAX_OBJECTIVE_SAMPLES = 8

_JUDGE_PROMPT = """你在评审一个自动生成的提案：把一段在历史会话中反复出现的工具调用序列，沉淀为一条可复用的技能（SOP）。统计门槛（跨会话复现、意图聚类、成功率）已经通过，不需要你复核数字。你要做的是统计规则做不了的**语义判断**。

# 证据

## 意图簇代表句
{representative}

## 用户实际提出过的请求（去重抽样，共 {objective_count} 条）
{objectives}

## 每次都复现的工具调用序列
{tool_sequence}

## 统计
出现于 {occasions} 次独立会话、{support} 个执行片段，成功率 {success_rate}。

# 你要判断的三件事

1. **这些请求真的属于同一类任务吗？** 意图聚类可能把措辞相近但目的不同的请求归并到一起。
2. **这个序列对这类任务携带有区分度的过程知识吗？** 像"先搜索再写文件"这种对任何任务都成立的通用组合，沉淀成 SOP 不提供任何指导价值，不值得占用一个技能位。
3. **固化后照做是否合理？** 下次遇到同类请求直接按这个序列执行，有没有明显不妥（比如中间步骤依赖当次会话的特殊上下文）。

# 输出

严格输出一个 JSON 对象，不要代码块、不要额外说明：

{{"worthy": true 或 false, "reason": "一句话结论，评审员可读，说明为什么值得/不值得沉淀", "trigger": "worthy 为 true 时：什么样的请求应当触发这条 SOP，一句话；否则留空字符串"}}
"""


@dataclass
class SopVerdict:
    """One adjudication, refusal-by-default."""

    worthy: bool
    reason: str
    trigger: str = ""
    # ``True`` when no judgement could be made because the model was
    # unreachable — an operational state, not a rejection, and the two must
    # not look alike in the report.
    judge_unavailable: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "worthy": self.worthy,
            "reason": self.reason,
            "trigger": self.trigger,
            "judge_unavailable": self.judge_unavailable,
        }


async def adjudicate_sequence_sop(
    *,
    representative: str,
    objectives: Sequence[str],
    tool_sequence: Sequence[str],
    occasions: int,
    support: int,
    success_rate: float,
) -> SopVerdict:
    """Ask the model whether a rule-surviving sequence is worth compiling.

    Returns a refusal on every uncertain path: unreachable model, unparseable
    output, missing fields. Passing on uncertainty is how single-tool noise
    became skills before the rule gates existed, and the same principle holds
    at this layer.
    """
    from core.memory.extractors._base import parse_json, run_llm_with_prompt

    sampled = [str(o).strip() for o in objectives if str(o).strip()]
    deduped: list = []
    for objective in sampled:
        if objective not in deduped:
            deduped.append(objective)
        if len(deduped) >= MAX_OBJECTIVE_SAMPLES:
            break

    prompt = _JUDGE_PROMPT.format(
        representative=str(representative).strip() or "（无）",
        objective_count=len(deduped),
        objectives="\n".join(f"- {o[:160]}" for o in deduped) or "（无）",
        tool_sequence=" → ".join(str(t) for t in tool_sequence),
        occasions=occasions,
        support=support,
        success_rate=f"{success_rate:.0%}",
    )
    raw = await run_llm_with_prompt(prompt, timeout_s=JUDGE_TIMEOUT_S, max_tokens=400)
    if raw is None:
        return SopVerdict(
            worthy=False, reason="判定模型不可用，按拒绝处理", judge_unavailable=True
        )

    data = parse_json(raw, require_key="worthy")
    if not isinstance(data, dict):
        logger.info("[sop-judge] unparseable verdict: %r", raw[:200])
        return SopVerdict(worthy=False, reason="判定输出无法解析，按拒绝处理")

    return SopVerdict(
        worthy=bool(data.get("worthy")),
        reason=str(data.get("reason") or "").strip()[:200],
        trigger=str(data.get("trigger") or "").strip()[:200],
    )
