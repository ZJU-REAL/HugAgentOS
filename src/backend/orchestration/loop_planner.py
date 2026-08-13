"""自主循环规划器 v2 —— 侦察式规划 + 运行中重规划。

旧规划链路（loop_evaluator.decompose_requirements）的问题：用 fast 模型、disable_tools、
看不到任何项目文件，凭目标文本一次盲拆定终身；拆错方向整个 run 陪葬（Codex goal 模式是
主模型带工具在 repo 里调查后再规划）。本模块把规划拆成三步，全部以「循环评审与规划」
模型角色（模型管理 → 角色分配 → loop_reviewer，未配置回落 main_agent）驱动：

  1. :func:`scout_workspace` —— 只读侦察 agent 绑到与 worker 相同的沙箱/项目，亲自
     ls/read/grep 摸清现状（已有文件、已有进展、缺口），产出一段结构化侦察纪要。
     纯任务型循环（无项目绑定）且 /workspace 为空时快速跳过，不烧一次 agent。
  2. :func:`plan_requirements` —— 侦察纪要 + 目标 → 需求账本。每条需求可附一条**只读
     check_cmd**（driver 亲自在沙箱执行，退出码 0 即机检达标——worker 无法作弊），
     恢复 Codex「exit code 是金标准」的混合验收；语义类需求拿不准就不附。
     简单目标允许只拆 1~2 条，不再强制 3~8 的拆解仪式。
  3. :func:`replan_remaining` —— 运行中有需求被 blocked 时，对「未通过的剩余部分」
     重拆（已通过的需求原样保留、不可撤销）。重拆次数由 driver 用护栏封顶，防止
     规划抖动本身变成死循环。

所有函数失败时返回兜底值（None/[]），绝不拖垮循环——driver 侧永远有旧链路可退。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from core.infra.logging import get_logger
from orchestration.loop_evaluator import GoalSpec, _parse_json_array_lenient

logger = get_logger(__name__)

# 侦察纪要长度上限（进入规划 prompt 与账本存档的截断值）
_SURVEY_CAP = 3000
_MODEL_ROLE = "loop_reviewer"


# ── 侦察：只读 agent 亲自摸一遍工作区/项目 ─────────────────────────────────────
async def _workspace_is_empty(session_id: str, user_id: str) -> bool:
    """纯任务型循环的快速短路：/workspace 还什么都没有就不值得起一次侦察 agent。"""
    from core.sandbox import ExecuteRequest, get_sandbox_provider

    try:
        res = await get_sandbox_provider().execute(
            ExecuteRequest(
                script_content="ls -A /workspace 2>/dev/null | grep -v '^\\.' | head -5",
                script_name="_loop_scout_ls.sh",
                language="bash",
                timeout=30,
                session_id=session_id,
                user_id=user_id,
            )
        )
        return not (res.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001 - 探测失败按「非空」处理，走完整侦察
        logger.info("[loop-plan] workspace probe failed (%s), assuming non-empty", exc)
        return False


async def scout_workspace(
    *,
    objective: str,
    session_id: str,
    user_id: str,
    project_ctx: Optional[Dict[str, Any]] = None,
    chat_id: Optional[str] = None,
    model_name: Optional[str] = None,
) -> str:
    """只读侦察：绑定 worker 同款沙箱/项目，摸清现状后输出结构化纪要（失败返回 ""）。"""
    if not project_ctx and await _workspace_is_empty(session_id, user_id):
        logger.info("[loop-plan] empty workspace, skip scouting")
        return ""

    from core.llm.agent_factory import create_agent_executor
    from core.llm.mcp_manager import close_clients
    from orchestration.streaming import StreamingAgent

    where = "用户选定的项目文件夹（站点/工程真实源码所在）" if project_ctx else "沙箱 /workspace"
    prompt = (
        "你是一个自主循环的**开工侦察员**（只读）。循环马上要围绕下面的目标开工，"
        f"你先亲自把{where}摸一遍，给规划器写一份侦察纪要。\n\n"
        f"## 目标\n{objective}\n\n"
        "## 侦察要求\n"
        "1. `ls` / glob 摸清有哪些文件与目录结构；挑与目标最相关的 3~6 个文件读关键部分"
        "（大文件用 head/grep/wc 抽样，禁止整读）。\n"
        "2. 判断：哪些目标要件**已经存在/已有雏形**，哪些**完全缺失**，有什么坑（依赖、"
        "格式、构建方式）。\n"
        "3. 只读取证，禁止创建/修改/删除任何文件。\n\n"
        "## 输出（≤600字，markdown）\n"
        "### 现状\n- 目录结构与关键文件一句话点评\n"
        "### 已有进展\n- 与目标相关的既有成果\n"
        "### 缺口\n- 距离目标还缺什么\n"
        "### 风险与建议\n- 构建/格式/依赖上的注意点，建议的切入顺序"
    )
    try:
        agent, clients = await create_agent_executor(
            current_user_id=user_id,
            model_name=model_name,
            model_role=_MODEL_ROLE,
            sandbox_session_id=session_id,
            project_ctx=project_ctx,
            chat_id=chat_id,
            enabled_skill_ids=[],
            isolated=True,
            read_only=True,
            allow_bash=True,
            max_iters=10,
            tool_result_limit=6000,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[loop-plan] scout spawn failed: %s", exc)
        return ""

    sa = StreamingAgent(agent, clients)
    text = ""
    try:
        async for et, payload in sa.stream(
            [{"role": "user", "content": prompt}],
            {"user_id": user_id, "model_name": model_name or "",
             "enable_thinking": False, "chat_mode": "medium"},
        ):
            if et == "text_delta":
                text += payload
            elif et == "error":
                logger.warning("[loop-plan] scout stream error: %s", payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[loop-plan] scout run failed: %s", exc)
    finally:
        await close_clients(clients)
    return text.strip()[:_SURVEY_CAP]


# ── 规划：侦察纪要 + 目标 → 需求账本（含可选 check_cmd） ────────────────────────
_CHECK_CMD_RULES = (
    "check_cmd 规则：**只读、幂等、90 秒内出结果**的 bash 命令，在工作区根目录执行，"
    "退出码 0 即该需求客观达标（如 `test -f xxx`、`grep -q '关键实现' 文件`、"
    "`[ $(wc -m < draft.md) -ge 50000 ]`、`grep -c '^# 第' draft.md | grep -qx 20`）。"
    "只有能用命令**客观判定**的需求才附；语义/质量类需求（写得好不好、逻辑是否通顺）"
    "一律省略该字段，交给评审员。禁止写任何会修改文件的命令。"
)


async def _plan_llm_once(prompt: str, *, model_name: Optional[str], user_id: str) -> str:
    """规划专用的一次性纯文本调用：loop_reviewer 角色 + medium 档（规划值得比 fast 更强的模型）。"""
    from core.llm.agent_factory import create_agent_executor
    from core.llm.mcp_manager import close_clients
    from orchestration.streaming import StreamingAgent

    agent, clients = await create_agent_executor(
        disable_tools=True,
        enabled_skill_ids=[],
        chat_mode="medium",
        model_name=model_name,
        model_role=_MODEL_ROLE,
        current_user_id=user_id,
    )
    sa = StreamingAgent(agent, clients)
    text = ""
    try:
        async for et, payload in sa.stream(
            [{"role": "user", "content": prompt}],
            {"user_id": user_id, "enable_thinking": False, "chat_mode": "medium"},
        ):
            if et == "text_delta":
                text += payload
            elif et == "error":
                logger.warning("[loop-plan] plan LLM error: %s", payload)
                break
    finally:
        await close_clients(clients)
    return text


def _sanitize_requirements(items: Optional[List[Any]], *, id_prefix: str = "R") -> List[Dict[str, Any]]:
    reqs: List[Dict[str, Any]] = []
    for i, raw in enumerate(items or [], start=1):
        if not isinstance(raw, dict):
            continue
        desc = str(raw.get("description", "")).strip()
        if not desc:
            continue
        entry: Dict[str, Any] = {"id": str(raw.get("id") or f"{id_prefix}{i}"), "description": desc}
        cmd = str(raw.get("check_cmd", "") or "").strip()
        if cmd:
            entry["check_cmd"] = cmd
        reqs.append(entry)
    return reqs


async def plan_requirements(
    *,
    goal_spec: GoalSpec,
    survey: str,
    model_name: Optional[str],
    user_id: str,
) -> List[Dict[str, Any]]:
    """侦察纪要 + 目标 → 需求账本。失败返回 []（driver 退回旧 decompose 链路）。"""
    criteria_block = (
        "已知验收标准（据此拆，勿遗漏）：\n"
        + "\n".join(f"- {c}" for c in goal_spec.acceptance_criteria) + "\n\n"
        if goal_spec.acceptance_criteria else ""
    )
    survey_block = (
        f"## 侦察纪要（侦察员亲自查看工作区后的实况，规划必须以此为准）\n{survey}\n\n"
        if survey else ""
    )
    prompt = (
        "你是一个自主循环的规划器。把目标拆成一组**离散、可独立核验**的需求账本，"
        "循环会一次只啃一条、逐条做扎实、逐条由独立评审员核验。\n\n"
        f"## 目标\n{goal_spec.objective}\n\n"
        + criteria_block
        + survey_block
        + "## 拆解规则\n"
        "1. 条数**按任务体量定**：简单目标 1~2 条即可，复杂目标最多 8 条；不要为了拆而拆。\n"
        "2. 每条是一个能客观判断「做没做到」的具体特性/改动/交付物；粒度适中。\n"
        "3. 若侦察纪要显示某要件**已存在且达标**，不要再立需求重做；在既有成果上补缺口。\n"
        "4. 按依赖与优先级排序（先地基后装修）。\n"
        f"5. {_CHECK_CMD_RULES}\n\n"
        "## 输出\n严格只输出 JSON 数组，每个元素形如 "
        '{"id":"R1","description":"...","check_cmd":"..."}（check_cmd 可省略）。'
        "不要任何多余文字。"
    )
    try:
        text = await _plan_llm_once(prompt, model_name=model_name, user_id=user_id)
        reqs = _sanitize_requirements(_parse_json_array_lenient(text))
        if reqs:
            return reqs[:8]
    except Exception as exc:  # noqa: BLE001 - 规划失败绝不拖垮循环
        logger.warning("[loop-plan] plan_requirements failed: %s", exc)
    return []


# ── 重规划：blocked 后对剩余部分重拆（passed 不动） ─────────────────────────────
async def replan_remaining(
    *,
    goal_spec: GoalSpec,
    ledger: Dict[str, Any],
    survey: str,
    model_name: Optional[str],
    user_id: str,
) -> Optional[List[Dict[str, Any]]]:
    """对未通过的剩余需求重拆。返回**完整的新账本需求列表**（已通过项原样保留在前），
    失败/无法改进时返回 None（driver 维持原账本）。"""
    passed = [r for r in ledger.get("requirements", []) if r.get("passes")]
    remaining = [r for r in ledger.get("requirements", []) if not r.get("passes")]
    if not remaining:
        return None

    def _fmt(r: Dict[str, Any]) -> str:
        flags = []
        if r.get("blocked"):
            flags.append(f"已尝试 {r.get('attempts', 0)} 轮未过被搁置")
        note = f"（{'；'.join(flags)}）" if flags else ""
        fb = str(r.get("last_feedback", "") or "")[:200]
        fb_line = f"\n  最近评审反馈：{fb}" if fb else ""
        return f"- {r['id']}: {r['description']}{note}{fb_line}"

    prompt = (
        "你是一个自主循环的规划器。循环执行中有需求多轮未通过评审被搁置，"
        "说明**原来的拆法或方向可能有问题**。请只对「剩余未完成部分」重新规划。\n\n"
        f"## 总目标\n{goal_spec.objective}\n\n"
        + (f"## 侦察纪要（工作区实况）\n{survey}\n\n" if survey else "")
        + "## 已通过的需求（**不许动**，重规划不得与其重复）\n"
        + ("\n".join(f"- {r['id']}: {r['description']}" for r in passed) or "（无）")
        + "\n\n## 未完成的需求（重规划对象，含搁置原因）\n"
        + "\n".join(_fmt(r) for r in remaining)
        + "\n\n## 重规划规则\n"
        "1. 剖析搁置原因：是需求太大（拆细）、方向错了（换方案）、还是环境根本做不到（改成可达成的等价目标）。\n"
        "2. 输出**替换全部未完成需求**的新需求列表（1~6 条），总目标不变、只换实现路径。\n"
        "3. 若你判断原拆法没问题、纯粹是执行没到位，输出原需求（可微调描述给出更具体的做法提示）。\n"
        f"4. {_CHECK_CMD_RULES}\n\n"
        "## 输出\n严格只输出 JSON 数组（新的未完成需求列表），元素形如 "
        '{"id":"N1","description":"...","check_cmd":"..."}。不要任何多余文字。'
    )
    try:
        text = await _plan_llm_once(prompt, model_name=model_name, user_id=user_id)
        fresh = _sanitize_requirements(_parse_json_array_lenient(text), id_prefix="N")
        if not fresh:
            return None
        # 已通过项原样保留在前，新需求接续其后；防 id 撞车
        used = {r["id"] for r in passed}
        for r in fresh:
            if r["id"] in used:
                r["id"] = f"N{len(used) + 1}"
            used.add(r["id"])
        return passed + fresh
    except Exception as exc:  # noqa: BLE001
        logger.warning("[loop-plan] replan failed: %s", exc)
        return None


def summarize_plan_for_log(reqs: List[Dict[str, Any]]) -> str:
    return json.dumps(
        [{"id": r["id"], "check": bool(r.get("check_cmd"))} for r in reqs],
        ensure_ascii=False,
    )
