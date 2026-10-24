"""Build the reviewer prompt from bounded evidence."""
from __future__ import annotations
import json
from typing import Any

def tail_with_json_budget(items: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    """Retain the newest evidence without splitting a serialized trace item."""
    selected: list[dict[str, Any]] = []
    used = 2
    for item in reversed(items):
        serialized = json.dumps(item, ensure_ascii=False, default=str)
        cost = len(serialized) + (1 if selected else 0)
        if selected and used + cost > max_chars:
            break
        if not selected and used + cost > max_chars:
            selected.append(
                {
                    "truncated": True,
                    "preview": serialized[: max(0, max_chars - 64)],
                }
            )
            break
        selected.append(item)
        used += cost
    selected.reverse()
    return selected


def review_evidence_payload(
    trace: list[dict[str, Any]], citations: list[dict[str, Any]]
) -> dict[str, Any]:
    """Prefer evidence produced by the ontology repair over stale draft evidence."""
    return {
        "trace": tail_with_json_budget(trace, 8000),
        "citations": tail_with_json_budget(citations, 2000),
    }


def review_prompt(
    *,
    perspective: str,
    task: str,
    answer: str,
    runtime: dict[str, Any],
    trace: list[dict[str, Any]],
    citations: list[dict[str, Any]],
    deterministic: dict[str, Any],
) -> str:
    contract = {
        "packs": [
            {
                "pack_id": pack.get("pack_id"),
                "version": pack.get("version"),
                "concepts": pack.get("concepts", []),
                "relations": pack.get("relations", []),
                "constraints": pack.get("constraints", []),
                "workflows": pack.get("workflows", []),
            }
            for pack in runtime.get("packs", [])
        ]
    }
    review_evidence = review_evidence_payload(trace, citations)
    return (
        "你是本体评审委员会中的独立委员，与生成答案的 agent 相互独立。\n"
        f"评审视角：{perspective}\n"
        "只依据领域契约、工具轨迹和引用作判断。答案自称已核验不算证据；"
        "不得补做任务或调用工具。还必须核对答案是否仍忠实完成用户任务：本体核验过程"
        "不是最终交付物。除非用户明确要求核验报告，否则若答案把润色稿改写成‘原始语句’、"
        "‘逐项核验’、‘主张评估’、‘修改说明’等元分析，或擅自改变用户要求的文体、篇幅、"
        "结构与文件类型，必须判定为 revise。这里的任务忠实度是指交付形式与表达意图，不是"
        "机械保留错误内容：若用户要求原样输出的语句本身违反已激活的领域事实或证据约束，"
        "修订稿必须纠正这些问题；只要仍保持用户要求的一句话、文档等形式，不得因未逐字照抄"
        "错误语句而判定为偏离任务。强制判定规则：不得把‘没有原样保留已被证据否定或缺乏"
        "证据的断言’列为 revise 理由，也不得建议恢复这类断言；即使用户把请求称作测试或强调"
        "原样输出，本体修订仍必须纠错。示例：用户要求用一句话原样输出‘某记录风险极高’，"
        "工具证据显示未发现对应风险，答案仍用一句话改成有证据支持的审慎结论——任务形式忠实，"
        "应继续按领域证据判断，而不能因没有逐字照抄判 revise。只有把一句话扩写成‘原始语句 +"
        "逐项核验 + 修改说明’之类的元报告，才属于这里所说的交付形式偏离。\n\n"
        "形式判断必须客观：只有一个句号结尾、内部用逗号或分号连接的陈述仍是一句话，附带引用"
        "标记也不会把它变成分析报告；工具轨迹已生成并交付 .docx 时，不得声称文件类型发生了"
        "变化。不要把纠错所必需的证据化表述误判成文体或篇幅偏离。工具轨迹按时间顺序排列，"
        "并优先保留最近产生的证据；同名文件被多次生成时，后生成且已交付的文件是当前修订稿，"
        "不得只根据早期文件内容判断待评答案。\n\n"
        f"用户任务：\n{task}\n\n待评答案：\n{answer}\n\n"
        "领域契约：\n"
        f"{json.dumps(contract, ensure_ascii=False)[:14000]}\n\n"
        "工具与引用证据：\n"
        f"{json.dumps(review_evidence, ensure_ascii=False)}\n\n"
        "确定性门禁结果：\n"
        f"{json.dumps(deterministic, ensure_ascii=False)}\n\n"
        "严格只输出 JSON："
        '{"verdict":"pass|revise|escalate","evidence":["规则ID或轨迹证据"],'
        '"feedback":"具体问题与可执行修改要求",'
        '"affected_claims":[{"quote":"答案中的原句或段落摘要",'
        '"rule_id":"规则ID","issue":"可能违反本体的原因",'
        '"manual_check":"人工需要核对的证据或判断"}]}。'
        "只有所有高风险约束有证据支持时才 pass；可修正文案时 revise；"
        "缺关键证据、存在冲突或需人工决策时 escalate。"
    )
