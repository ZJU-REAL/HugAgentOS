"""Real-LLM A/B for a materialised evolution skill.

    PYTHONPATH=src/backend python -m scripts.run_evolution_llm_ab [--skill-id evo-…]

The offline benchmark (``run_evolution_benchmark``) answers a narrower question
than people read into it: it holds a deterministic planner fixed and measures
whether the harness now *holds* a reusable capability.  It cannot answer whether
a real model, handed that capability, actually plans differently — the planner is
a stand-in, and a stand-in that always obeys a rule proves nothing about
obedience.

This script closes that gap.  Both arms are real calls to a configured chat
provider on the identical task set; the only difference is whether the skill
document the evolution pipeline wrote is present:

* **arm A** — the task alone.
* **arm B** — the task plus the materialised ``SKILL.md``, verbatim, as the
  runtime delivers it once the model has performed the mandated read step.

Three families, and the interesting one is not the first:

* ``validated_in`` families — the rule claims to apply. Improvement expected.
* ``contradicted_in`` families — the rule claims **not** to apply, and says so in
  its own text. If the model applies it anyway, the scope annotation is
  decorative and the capability is actively harmful there. This is the
  over-application check, and it is the reason the trap family exists.
* distractor family — unrelated ordering. Movement here means the skill is
  changing behaviour it has no business changing.

What it still does not model: the tool-execution loop, and the decision to open
the skill in the first place (arm B assumes the read happened). Both are stated
rather than smoothed over.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

TIMEOUT_S = 90
# Deterministic decoding: the question is whether the skill changes the plan, and
# sampling noise would be indistinguishable from a small real effect at this
# sample size.
TEMPERATURE = 0.0


@dataclass(frozen=True)
class LlmTask:
    task_id: str
    family: str
    prompt: str
    tools: Tuple[str, ...]
    # (first, second): ``first`` must be called before ``second`` to succeed.
    ordering: Tuple[str, str]


def _listing(tools: Sequence[Tuple[str, str]], task_id: str, ordering: Tuple[str, str]) -> str:
    """Render the available-tools list with the constrained pair counterbalanced.

    Without this the whole experiment measures nothing. A first run scored 100%
    in **both** arms on every family, which looked like "the model already knows
    the ordering" — it was not. The prompt listed the tools in the correct order,
    so a model that simply echoes the listing scores perfectly while
    demonstrating no knowledge at all.

    Half the tasks therefore list the constrained pair the right way round and
    half the wrong way round, deterministically by task id. Copying the listing
    now scores ~50%, so the baseline has headroom and the arms differ by
    knowledge rather than by transcription.
    """
    first, second = ordering
    items = list(tools)
    flip = int(task_id.rsplit("-", 1)[-1]) % 2 == 1
    if flip:
        index = {name: i for i, (name, _) in enumerate(items)}
        if first in index and second in index:
            i, j = index[first], index[second]
            items[i], items[j] = items[j], items[i]
    return "、".join(f"{name}（{desc}）" for name, desc in items)


def build_task_set() -> List[LlmTask]:
    """Suite ``business`` — realistic tasks whose correct order is *intuitive*.

    Measured result: a current model scores 100% on these unaided, so the suite
    has no headroom and can prove nothing about obedience. That is itself the
    finding — for orderings a competent model already derives, a distilled rule
    adds nothing, and the offline benchmark's effect comes from modelling the
    planner as ignorant of them.

    Kept rather than deleted because it is the control: it is what shows the
    ``platform`` suite's effect is not an artefact of prompt shape.
    """
    tasks: List[LlmTask] = []

    # ── validated_in: 产业周报 ──
    for i in range(8):
        tasks.append(
            LlmTask(
                task_id=f"report-{i:02d}",
                family="industry_report",
                prompt=(
                    f"任务：生成第 {i + 1} 期新能源汽车市场周报。"
                    "要求报告中出现的企业主体必须准确，并附可追溯的数据来源。\n"
                    "可用工具：company_verify（企业主体核验）、db_query（产业数据查询）、"
                    "chart_render（图表渲染）、doc_export（文档导出）。"
                ),
                tools=("company_verify", "db_query", "chart_render", "doc_export"),
                ordering=("company_verify", "db_query"),
            )
        )

    # ── validated_in: 财务分析（同一顺序教训，不同工具集） ──
    for i in range(8):
        tasks.append(
            LlmTask(
                task_id=f"finance-{i:02d}",
                family="finance",
                prompt=(
                    f"任务：分析目标企业 {i + 1} 的财务风险并给出风险结论。\n"
                    "可用工具：company_verify（企业主体核验）、db_query（财务数据查询）、"
                    "risk_score（风险打分）。"
                ),
                tools=("company_verify", "db_query", "risk_score"),
                ordering=("company_verify", "db_query"),
            )
        )

    # ── contradicted_in: 该场景下顺序相反才对 ──
    for i in range(8):
        tasks.append(
            LlmTask(
                task_id=f"trap-{i:02d}",
                family="trap",
                prompt=(
                    f"任务：内部流程草稿 {i + 1}。本场景的业务规则是——"
                    "**核验环节需要以查询结果作为输入**（先拿到数据，才能核验数据里出现的主体），"
                    "请据此安排步骤。\n"
                    "可用工具：db_query（数据查询）、company_verify（主体核验）、"
                    "doc_export（文档导出）。"
                ),
                tools=("db_query", "company_verify", "doc_export"),
                ordering=("db_query", "company_verify"),
            )
        )

    # ── distractor: 与任何规则无关 ──
    for i in range(6):
        tasks.append(
            LlmTask(
                task_id=f"kbqa-{i:02d}",
                family="kb_qa",
                prompt=(
                    f"任务：回答知识库问题 {i + 1}，答案需要给出引用。\n"
                    "可用工具：kb_search（知识库检索）、cite_check（引用校验）。"
                ),
                tools=("kb_search", "cite_check"),
                ordering=("kb_search", "cite_check"),
            )
        )
    return tasks


def build_platform_task_set() -> List[LlmTask]:
    """Suite ``platform`` — ordering the model cannot derive, only learn.

    ``currency_resolve`` must precede ``region_resolve`` on this platform. There
    is no semantic reason: they are peer resolvers and either order reads as
    plausible, so a model has no prior and lands near chance. That is exactly the
    class of knowledge harness evolution is supposed to capture — a
    platform-specific convention no pre-training could contain — and therefore
    the only class on which "did the learned rule change the plan?" is a
    measurable question.

    The trap family reverses it, and the rule's own text names that family as an
    exception. If the model applies the rule there anyway, the scope annotation
    is decorative.
    """
    tasks: List[LlmTask] = []

    settle_tools = (
        ("currency_resolve", "币种解析"),
        ("region_resolve", "区域解析"),
        ("metric_compute", "指标计算"),
        ("report_render", "报告渲染"),
    )
    for i in range(12):
        tid = f"settle-{i:02d}"
        order = ("currency_resolve", "region_resolve")
        tasks.append(
            LlmTask(
                task_id=tid,
                family="settlement",
                prompt=(
                    f"任务：为跨境结算批次 {i + 1} 生成对账报告。\n"
                    f"可用工具：{_listing(settle_tools, tid, order)}。\n"
                    "请给出调用顺序。"
                ),
                tools=tuple(name for name, _ in settle_tools),
                ordering=order,
            )
        )

    # Same lesson, different tool set: this is the transfer claim.
    pricing_tools = (
        ("currency_resolve", "币种解析"),
        ("region_resolve", "区域解析"),
        ("price_adjust", "调价计算"),
    )
    for i in range(10):
        tid = f"pricing-{i:02d}"
        order = ("currency_resolve", "region_resolve")
        tasks.append(
            LlmTask(
                task_id=tid,
                family="pricing",
                prompt=(
                    f"任务：为区域定价方案 {i + 1} 计算调价建议。\n"
                    f"可用工具：{_listing(pricing_tools, tid, order)}。\n"
                    "请给出调用顺序。"
                ),
                tools=tuple(name for name, _ in pricing_tools),
                ordering=order,
            )
        )

    legacy_tools = (
        ("region_resolve", "区域解析"),
        ("currency_resolve", "币种解析"),
        ("report_render", "报告渲染"),
    )
    for i in range(10):
        tid = f"legacy-{i:02d}"
        order = ("region_resolve", "currency_resolve")
        tasks.append(
            LlmTask(
                task_id=tid,
                family="legacy_batch",
                prompt=(
                    f"任务：处理历史批次 {i + 1} 的补算。\n"
                    f"可用工具：{_listing(legacy_tools, tid, order)}。\n"
                    "请给出调用顺序。"
                ),
                tools=tuple(name for name, _ in legacy_tools),
                ordering=order,
            )
        )

    kb_tools = (("kb_search", "知识库检索"), ("cite_check", "引用校验"))
    for i in range(8):
        tid = f"kbqa-{i:02d}"
        order = ("kb_search", "cite_check")
        tasks.append(
            LlmTask(
                task_id=tid,
                family="kb_qa",
                prompt=(
                    f"任务：回答知识库问题 {i + 1}，答案需要给出引用。\n"
                    f"可用工具：{_listing(kb_tools, tid, order)}。"
                ),
                tools=tuple(name for name, _ in kb_tools),
                ordering=order,
            )
        )
    return tasks


SUITES = {"business": build_task_set, "platform": build_platform_task_set}

# Which families each suite's rule claims to cover, and which it disclaims. Used
# only for reporting — the rule's own scope lives in the skill document.
SUITE_FAMILIES = {
    "business": {
        "applicable": ("industry_report", "finance"),
        "contradicted": ("trap",),
        "distractor": ("kb_qa",),
    },
    "platform": {
        "applicable": ("settlement", "pricing"),
        "contradicted": ("legacy_batch",),
        "distractor": ("kb_qa",),
    },
}


_INSTRUCTION = (
    "你是一个任务规划器。请只规划工具调用顺序，不要执行、不要解释。\n"
    "输出格式：一个 JSON 数组，元素是工具名字符串，按调用先后排列。"
    "只输出 JSON，不要出现其它文字或代码块标记。"
)


def build_messages(task: LlmTask, skill_document: Optional[str]) -> List[Dict[str, str]]:
    """The two arms differ by exactly one block.

    Arm B mirrors the runtime's shape: the skill is announced as an available
    skill and its instructions follow, which is the state the model is in after
    the mandated ``view_text_file`` read.
    """
    system = _INSTRUCTION
    if skill_document:
        system += (
            "\n\n# 技能（Agent Skills）\n"
            "以下是当前可用的技能。技能不是工具，不能直接调用；"
            "当任务匹配技能描述时，你必须遵循技能中的指令。\n\n"
            "<skill>\n" + skill_document + "\n</skill>"
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": task.prompt},
    ]


_JSON_ARRAY = re.compile(r"\[[^\[\]]*\]", re.S)


def parse_plan(text: str, tools: Sequence[str]) -> List[str]:
    """Extract the planned tool order from the model's reply.

    Falls back to first-mention order when the reply is not valid JSON: refusing
    to parse a legible answer would score a formatting failure as an ordering
    failure, and this measures ordering.
    """
    raw = (text or "").strip()
    for candidate in ([raw] if raw.startswith("[") else []) + _JSON_ARRAY.findall(raw):
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, list):
            names = [str(x).strip().strip("`") for x in parsed]
            keep = [n for n in names if n in set(tools)]
            if keep:
                return keep
    positions = [(raw.find(tool), tool) for tool in tools if raw.find(tool) >= 0]
    return [tool for _, tool in sorted(positions)]


def satisfies(plan: Sequence[str], ordering: Tuple[str, str]) -> bool:
    first, second = ordering
    if first not in plan or second not in plan:
        return False
    return plan.index(first) < plan.index(second)


# ── Provider access ──────────────────────────────────────────────────────────


def pick_provider(preferred: Optional[str] = None) -> Tuple[str, str, str, str]:
    """(display_name, model_name, base_url, api_key) for an active chat provider.

    Reads the same ``model_providers`` rows the product serves traffic from, so
    the arms run against a real configured endpoint rather than a test double.
    """
    from core.db.engine import SessionLocal
    from core.db.models import ModelProvider

    with SessionLocal() as db:
        query = db.query(ModelProvider).filter(
            ModelProvider.provider_type == "chat", ModelProvider.is_active == True  # noqa: E712
        )
        rows = query.order_by(ModelProvider.priority.desc()).all()
    if not rows:
        raise SystemExit("no active chat provider configured")
    if preferred:
        for row in rows:
            if preferred in (row.model_name, row.provider_id, row.display_name):
                return row.display_name, row.model_name, row.base_url, row.api_key
        raise SystemExit(f"provider {preferred!r} not found among {[r.model_name for r in rows]}")
    return rows[0].display_name, rows[0].model_name, rows[0].base_url, rows[0].api_key


async def call_model(
    client: Any, *, model: str, base_url: str, api_key: str, messages: List[Dict[str, str]]
) -> str:
    response = await client.post(
        base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": messages,
            "temperature": TEMPERATURE,
            "max_tokens": 400,
        },
    )
    response.raise_for_status()
    payload = response.json()
    message = payload["choices"][0]["message"]
    # Reasoning providers put the answer in ``content`` and their trace elsewhere;
    # concatenating would feed the trace into the parser.
    return message.get("content") or ""


# ── Skill document ───────────────────────────────────────────────────────────


def load_skill_document(skill_id: Optional[str]) -> Tuple[str, str]:
    """The materialised SKILL.md, read from the asset the pipeline actually wrote."""
    from core.db.engine import SessionLocal
    from core.db.models import AdminSkill
    from core.evolution.activation import EVOLUTION_PREFIX

    with SessionLocal() as db:
        query = db.query(AdminSkill)
        if skill_id:
            row = query.filter(AdminSkill.skill_id == skill_id).first()
        else:
            row = (
                query.filter(AdminSkill.skill_id.like(f"{EVOLUTION_PREFIX}%"))
                .order_by(AdminSkill.updated_at.desc())
                .first()
            )
        if row is None:
            raise SystemExit(
                "no materialised evolution skill found — run the loop and activate a "
                "candidate first, or pass --skill-id"
            )
        return row.skill_id, row.skill_content or ""


# ── Scoring ──────────────────────────────────────────────────────────────────


@dataclass
class ArmResult:
    name: str
    passed: Dict[str, bool] = field(default_factory=dict)
    plans: Dict[str, List[str]] = field(default_factory=dict)
    errors: Dict[str, str] = field(default_factory=dict)

    def rate(self, family: Optional[str] = None, tasks: Sequence[LlmTask] = ()) -> float:
        ids = [t.task_id for t in tasks if family is None or t.family == family]
        scored = [self.passed[i] for i in ids if i in self.passed]
        return sum(scored) / len(scored) if scored else 0.0


def compare(before: ArmResult, after: ArmResult, tasks: Sequence[LlmTask]) -> Dict[str, Any]:
    from core.evolution.replay import mcnemar_p_value

    shared = [t.task_id for t in tasks if t.task_id in before.passed and t.task_id in after.passed]
    regressed = [t for t in shared if before.passed[t] and not after.passed[t]]
    improved = [t for t in shared if after.passed[t] and not before.passed[t]]
    return {
        "n_paired": len(shared),
        "before_rate": round(sum(before.passed[t] for t in shared) / len(shared), 4) if shared else 0.0,
        "after_rate": round(sum(after.passed[t] for t in shared) / len(shared), 4) if shared else 0.0,
        "effect_size": round(
            (sum(after.passed[t] for t in shared) - sum(before.passed[t] for t in shared))
            / len(shared),
            4,
        )
        if shared
        else 0.0,
        "p_value": round(mcnemar_p_value(len(regressed), len(improved)), 6),
        "improved": improved,
        "regressed": regressed,
    }


async def run_arm(
    name: str,
    tasks: Sequence[LlmTask],
    skill_document: Optional[str],
    *,
    model: str,
    base_url: str,
    api_key: str,
    concurrency: int = 4,
) -> ArmResult:
    import httpx

    result = ArmResult(name)
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:

        async def one(task: LlmTask) -> None:
            async with semaphore:
                try:
                    text = await call_model(
                        client,
                        model=model,
                        base_url=base_url,
                        api_key=api_key,
                        messages=build_messages(task, skill_document),
                    )
                except Exception as exc:  # noqa: BLE001
                    result.errors[task.task_id] = f"{type(exc).__name__}: {exc}"
                    return
                plan = parse_plan(text, task.tools)
                result.plans[task.task_id] = plan
                # An unparseable reply is recorded as a failure rather than
                # dropped: silently excluding it would let a model that answers
                # incoherently under one arm look better than it is.
                result.passed[task.task_id] = satisfies(plan, task.ordering)

        await asyncio.gather(*[one(task) for task in tasks])
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-id", default=None, help="要测的已物化技能 id")
    parser.add_argument("--model", default=None, help="model_name / provider_id / display_name")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--json-out", default=None, help="把完整结果写到该路径")
    parser.add_argument(
        "--suite",
        default="platform",
        choices=sorted(SUITES),
        help="business = 顺序直觉可推导（对照组，基线满分）；"
        "platform = 平台特有约定，模型无先验（可测组）",
    )
    args = parser.parse_args(argv)

    skill_id, document = load_skill_document(args.skill_id)
    display, model, base_url, api_key = pick_provider(args.model)
    tasks = SUITES[args.suite]()
    roles = SUITE_FAMILIES[args.suite]

    print(f"provider : {display}  ({model})")
    print(f"skill    : {skill_id}  ({len(document)} chars)")
    print(f"suite    : {args.suite}")
    print(f"tasks    : {len(tasks)}  families: {sorted({t.family for t in tasks})}")
    print(f"arms     : A = 任务; B = 任务 + 该技能文档\n")

    arm_a = asyncio.run(
        run_arm("A_no_skill", tasks, None, model=model, base_url=base_url,
                api_key=api_key, concurrency=args.concurrency)
    )
    arm_b = asyncio.run(
        run_arm("B_with_skill", tasks, document, model=model, base_url=base_url,
                api_key=api_key, concurrency=args.concurrency)
    )

    for arm in (arm_a, arm_b):
        if arm.errors:
            print(f"[{arm.name}] {len(arm.errors)} call(s) failed: "
                  f"{list(arm.errors.items())[:2]}")

    families = sorted({t.family for t in tasks})
    print(f"{'family':<18}{'A':>8}{'B':>8}{'delta':>9}   note")
    role_note = {
        "applicable": "规则声称适用 — 期望提升",
        "contradicted": "规则声称【不】适用 — B 变差即为过度套用",
        "distractor": "干扰组 — 任何变化都说明技能越界",
    }
    family_role = {
        family: role
        for role, names in roles.items()
        for family in names
    }
    for family in families:
        a = arm_a.rate(family, tasks)
        b = arm_b.rate(family, tasks)
        note = role_note.get(family_role.get(family, ""), "")
        print(f"{family:<18}{a:>7.1%}{b:>8.1%}{b - a:>+9.1%}   {note}")

    verdict_all = compare(arm_a, arm_b, tasks)
    applicable = [t for t in tasks if t.family in roles["applicable"]]
    verdict_applicable = compare(arm_a, arm_b, applicable)
    trap = [t for t in tasks if t.family in roles["contradicted"]]
    verdict_trap = compare(arm_a, arm_b, trap)

    print("\n── 全部任务 ──")
    print(json.dumps(verdict_all, ensure_ascii=False))
    print("\n── 规则声称适用的家族 ──")
    print(json.dumps(verdict_applicable, ensure_ascii=False))
    print("\n── 反例家族（过度套用检查）──")
    print(json.dumps(verdict_trap, ensure_ascii=False))

    over_applied = len(verdict_trap["regressed"])
    print("\n── 结论 ──")
    if verdict_applicable["effect_size"] > 0:
        print(f"· 模型确实按技能里的顺序约束改变了规划："
              f"适用家族 {verdict_applicable['before_rate']:.1%} → "
              f"{verdict_applicable['after_rate']:.1%}"
              f"（p={verdict_applicable['p_value']}）")
    else:
        print("· 在规则声称适用的家族上没有可测提升——技能文本没有改变模型的规划。")
    if over_applied:
        print(f"· ⚠ 反例家族有 {over_applied} 题回归：模型把规则套用到了它自己声明【不】适用的场景。"
              "作用域标注对该模型无效。")
    else:
        print("· 反例家族无回归：模型尊重了规则自带的作用域标注，没有外推。")
    distractors = [t for t in tasks if t.family in roles["distractor"]]
    if distractors:
        moved = [t.task_id for t in distractors
                 if arm_a.passed.get(t.task_id) != arm_b.passed.get(t.task_id)]
        print(f"· 干扰组变化 {len(moved)} 题" + ("（应为 0）" if moved else "，未越界。"))
    if verdict_applicable["before_rate"] >= 1.0:
        print("· ⚠ 基线已满分：该 suite 无上升空间，测不出服从性。"
              "说明这条顺序对当前模型是可自行推导的——换 --suite platform 才有可测空间。")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "provider": {"display": display, "model": model},
                    "skill_id": skill_id,
                    "arm_a": {"passed": arm_a.passed, "plans": arm_a.plans, "errors": arm_a.errors},
                    "arm_b": {"passed": arm_b.passed, "plans": arm_b.plans, "errors": arm_b.errors},
                    "verdict_all": verdict_all,
                    "verdict_applicable": verdict_applicable,
                    "verdict_trap": verdict_trap,
                },
                handle,
                ensure_ascii=False,
                indent=1,
            )
        print(f"\n完整结果已写入 {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
