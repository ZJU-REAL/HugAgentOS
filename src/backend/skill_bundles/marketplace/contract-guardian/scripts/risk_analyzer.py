#!/usr/bin/env python3
"""
contract-guardian 风险条款识别模块

基于关键词和正则模式匹配，识别合同中的 12 类风险条款。
免费版仅识别 3 类基础风险，付费版识别全部 12 类。
"""

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import (
    check_subscription,
    output_error,
    output_success,
    read_text_input,
)


# ============================================================
# 12 类风险条款定义
# ============================================================

RISK_CATEGORIES: List[Dict[str, Any]] = [
    {
        "id": "unilateral_termination",
        "name": "单方解约权",
        "severity": "high",
        "description": "一方可单方面解除合同，可能导致对方损失无法补偿",
        "keywords": [
            "单方解除", "单方终止", "有权解除", "可随时终止",
            "有权单方", "无需对方同意.*解除", "单方面解约",
        ],
        "patterns": [
            r"[甲乙丙]方有权.*(?:单方|随时).*(?:解除|终止)",
            r"(?:任何一方|任一方).*(?:无需|不需).*(?:同意|通知).*(?:解除|终止)",
        ],
        "recommendation": "建议增加解约条件限制，明确双方解约权的对等性和提前通知期",
        "free_tier": True,
    },
    {
        "id": "auto_renewal",
        "name": "自动续约",
        "severity": "medium",
        "description": "合同到期自动续约，未及时通知可能导致被动续约",
        "keywords": [
            "自动续约", "自动续期", "自动延长", "自动延续",
            "视为同意续约", "默认续期",
        ],
        "patterns": [
            r"(?:期满|到期).*(?:自动|默认).*(?:续约|续期|延长|延续)",
            r"(?:未.*(?:书面|提前).*(?:通知|提出)).*(?:视为|默认).*(?:续约|续期)",
        ],
        "recommendation": "建议明确续约通知期限（如提前30天书面通知），避免被动续约",
        "free_tier": False,
    },
    {
        "id": "unlimited_liability",
        "name": "无限责任",
        "severity": "high",
        "description": "一方承担无限制的赔偿责任，风险敞口过大",
        "keywords": [
            "无限责任", "全部损失", "一切损失", "全额赔偿",
            "承担所有", "不设上限", "无上限",
        ],
        "patterns": [
            r"(?:赔偿|承担).*(?:全部|一切|所有|任何).*(?:损失|损害|费用)",
            r"(?:责任|赔偿).*(?:不设|无|没有).*(?:上限|限制|限额)",
        ],
        "recommendation": "建议设定赔偿上限（如合同总金额的一定比例），限制责任范围",
        "free_tier": False,
    },
    {
        "id": "non_compete",
        "name": "竞业限制",
        "severity": "high",
        "description": "竞业限制条款可能过度约束经营自由",
        "keywords": [
            "竞业限制", "竞业禁止", "同业竞争", "不得从事",
            "不得经营", "竞争业务",
        ],
        "patterns": [
            r"(?:不得|禁止).*(?:从事|经营|参与).*(?:同类|类似|相同|竞争)",
            r"竞业.*(?:限制|禁止).*(?:\d+.*(?:年|月))",
        ],
        "recommendation": "建议明确竞业限制的范围、期限和补偿标准，确保合理性",
        "free_tier": False,
    },
    {
        "id": "ip_ownership",
        "name": "知识产权归属",
        "severity": "high",
        "description": "知识产权归属不明确，可能导致成果归属争议",
        "keywords": [
            "知识产权归", "著作权归", "专利归", "成果归属",
            "版权归属", "全部权利归",
        ],
        "patterns": [
            r"(?:知识产权|著作权|专利|版权|成果).*(?:归|属于).*[甲乙丙]方.*(?:所有|独有|享有)",
            r"(?:工作成果|开发成果|技术成果).*(?:全部|一切).*(?:归|属于)",
        ],
        "recommendation": "建议明确约定各方知识产权的归属范围，区分已有IP和新创IP",
        "free_tier": False,
    },
    {
        "id": "jurisdiction",
        "name": "管辖地/仲裁",
        "severity": "medium",
        "description": "争议解决管辖地可能对一方不利",
        "keywords": [
            "管辖", "仲裁", "诉讼管辖", "争议解决",
            "仲裁委员会", "仲裁机构",
        ],
        "patterns": [
            r"(?:由|向).*(?:人民法院|仲裁委员会|仲裁机构).*(?:管辖|仲裁|裁决)",
            r"(?:争议|纠纷).*(?:提交|申请).*(?:仲裁|诉讼)",
        ],
        "recommendation": "建议选择对双方公平的管辖地，优先考虑仲裁方式解决争议",
        "free_tier": False,
    },
    {
        "id": "confidentiality",
        "name": "保密条款",
        "severity": "medium",
        "description": "保密义务范围过广或期限过长",
        "keywords": [
            "保密义务", "保密期限", "保密责任", "保密信息",
            "商业秘密", "不得泄露",
        ],
        "patterns": [
            r"保密.*(?:期限|义务).*(?:永久|无限期|\d+.*年)",
            r"(?:一切|全部|所有).*(?:信息|资料).*(?:均为|视为).*(?:保密|机密)",
        ],
        "recommendation": "建议明确保密信息的范围和期限，避免过度约束",
        "free_tier": False,
    },
    {
        "id": "payment_terms",
        "name": "付款条件",
        "severity": "medium",
        "description": "付款条件不明确或对一方明显不利",
        "keywords": [
            "付款条件", "付款方式", "付款期限", "结算方式",
            "账期", "收到发票后",
        ],
        "patterns": [
            r"(?:付款|支付|结算).*(?:条件|前提|前置)",
            r"(?:收到.*(?:发票|验收报告|确认)).*(?:\d+.*(?:天|日|工作日)).*(?:内.*(?:付款|支付))",
        ],
        "recommendation": "建议明确付款时间节点、金额和条件，设置合理的账期",
        "free_tier": True,
    },
    {
        "id": "acceptance_criteria",
        "name": "验收标准",
        "severity": "medium",
        "description": "验收标准模糊，可能导致验收争议",
        "keywords": [
            "验收标准", "验收条件", "交付标准", "合格标准",
            "视为验收合格", "默认验收",
        ],
        "patterns": [
            r"(?:\d+.*(?:天|日|工作日)).*(?:内.*未.*(?:提出|提交).*(?:异议|问题)).*(?:视为|默认).*(?:合格|验收)",
            r"(?:验收|交付).*(?:标准|条件).*(?:由|以).*[甲乙丙]方.*(?:确定|为准)",
        ],
        "recommendation": "建议细化验收标准和流程，明确验收期限和异议处理机制",
        "free_tier": False,
    },
    {
        "id": "penalty",
        "name": "违约金",
        "severity": "high",
        "description": "违约金条款不对等或金额过高",
        "keywords": [
            "违约金", "违约责任", "违约赔偿", "逾期违约",
            "迟延履行", "日万分之",
        ],
        "patterns": [
            r"违约金.*(?:为|按).*(?:合同.*(?:总[额价金]|金额)).*(?:\d+%)",
            r"(?:每[日天]|日).*(?:万分之|千分之|百分之).*(?:\d+).*(?:违约金|滞纳金)",
        ],
        "recommendation": "建议违约金比例合理（通常不超过合同总额的30%），确保双方对等",
        "free_tier": True,
    },
    {
        "id": "guarantee",
        "name": "担保条款",
        "severity": "high",
        "description": "担保范围过广或担保方式不当",
        "keywords": [
            "担保", "保证金", "质押", "抵押", "连带责任",
            "无限连带", "担保责任",
        ],
        "patterns": [
            r"(?:无限|连带).*(?:担保|保证).*(?:责任)",
            r"(?:担保|保证).*(?:范围|期限).*(?:包括|涵盖).*(?:全部|一切|所有)",
        ],
        "recommendation": "建议明确担保范围、期限和方式，避免无限连带担保",
        "free_tier": False,
    },
    {
        "id": "force_majeure",
        "name": "不可抗力",
        "severity": "low",
        "description": "不可抗力条款定义过窄或免责范围过广",
        "keywords": [
            "不可抗力", "自然灾害", "政府行为", "战争",
            "疫情", "罢工",
        ],
        "patterns": [
            r"不可抗力.*(?:包括但不限于|包括)",
            r"(?:不可抗力|意外事件).*(?:免除|免于|不承担).*(?:全部|一切|任何).*(?:责任|义务)",
        ],
        "recommendation": "建议明确不可抗力的定义范围、通知义务和减损义务",
        "free_tier": False,
    },
]

# 免费版可用的风险类别 ID
FREE_TIER_CATEGORIES = [c["id"] for c in RISK_CATEGORIES if c.get("free_tier")]


def get_available_categories() -> List[Dict[str, Any]]:
    """根据订阅等级获取可用的风险类别。

    Returns:
        可用风险类别列表。
    """
    sub = check_subscription()
    if sub["tier"] == "paid":
        return RISK_CATEGORIES[:]

    return [c for c in RISK_CATEGORIES if c["id"] in FREE_TIER_CATEGORIES]


def analyze_risk(text: str, categories: List[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """分析合同文本中的风险条款。

    Args:
        text: 合同文本内容。
        categories: 要检查的风险类别列表，默认为当前订阅等级可用类别。

    Returns:
        风险项列表，每项包含:
        {
            "category_id": str,
            "category_name": str,
            "severity": str,
            "matched_text": str,
            "description": str,
            "recommendation": str
        }
    """
    if categories is None:
        categories = get_available_categories()

    risks = []

    for category in categories:
        matches = []

        # 关键词匹配
        for keyword in category["keywords"]:
            for match in re.finditer(keyword, text):
                # 提取匹配位置前后的上下文
                start = max(0, match.start() - 30)
                end = min(len(text), match.end() + 30)
                context = text[start:end].replace("\n", " ").strip()
                matches.append(context)

        # 正则模式匹配
        for pattern in category["patterns"]:
            try:
                for match in re.finditer(pattern, text):
                    start = max(0, match.start() - 20)
                    end = min(len(text), match.end() + 20)
                    context = text[start:end].replace("\n", " ").strip()
                    matches.append(context)
            except re.error:
                continue

        if matches:
            # 去重
            unique_matches = list(dict.fromkeys(matches))
            risks.append({
                "category_id": category["id"],
                "category_name": category["name"],
                "severity": category["severity"],
                "matched_text": unique_matches[:3],  # 最多保留3条匹配
                "description": category["description"],
                "recommendation": category["recommendation"],
            })

    # 按严重程度排序
    severity_order = {"high": 0, "medium": 1, "low": 2}
    risks.sort(key=lambda r: severity_order.get(r["severity"], 99))

    return risks


def quick_check(text: str) -> Dict[str, Any]:
    """快速检查合同中的主要风险。

    仅检查高风险类别，返回简要结果。

    Args:
        text: 合同文本内容。

    Returns:
        包含快速检查结果的字典。
    """
    categories = get_available_categories()
    high_risk_categories = [c for c in categories if c["severity"] == "high"]
    risks = analyze_risk(text, high_risk_categories)

    return {
        "total_high_risks": len(risks),
        "risk_level": "高" if len(risks) >= 3 else "中" if len(risks) >= 1 else "低",
        "risks": risks,
        "summary": _generate_risk_summary(risks),
    }


def full_report(text: str) -> Dict[str, Any]:
    """生成完整的风险分析报告。

    Args:
        text: 合同文本内容。

    Returns:
        包含完整分析结果的字典。
    """
    categories = get_available_categories()
    risks = analyze_risk(text, categories)
    sub = check_subscription()

    high_risks = [r for r in risks if r["severity"] == "high"]
    medium_risks = [r for r in risks if r["severity"] == "medium"]
    low_risks = [r for r in risks if r["severity"] == "low"]

    # 计算风险评分（0-100，越高越安全）
    total_categories = len(categories)
    risk_count = len(risks)
    weighted_score = (
        len(high_risks) * 3 + len(medium_risks) * 2 + len(low_risks) * 1
    )
    max_weighted = total_categories * 3
    safety_score = max(0, round(100 - (weighted_score / max_weighted * 100)))

    report = {
        "tier": sub["tier"],
        "categories_checked": len(categories),
        "total_categories": 12,
        "total_risks_found": risk_count,
        "high_risk_count": len(high_risks),
        "medium_risk_count": len(medium_risks),
        "low_risk_count": len(low_risks),
        "safety_score": safety_score,
        "risk_level": _get_risk_level(safety_score),
        "risks": risks,
        "summary": _generate_risk_summary(risks),
    }

    if sub["tier"] == "free":
        report["upgrade_hint"] = (
            f"当前为免费版，仅检查了 {len(categories)}/{12} 类风险。"
            f"升级至付费版（¥129/月）可检查全部 12 类风险条款。"
        )

    return report


def _get_risk_level(score: int) -> str:
    """根据安全评分获取风险等级。"""
    if score >= 80:
        return "低风险"
    elif score >= 60:
        return "中等风险"
    elif score >= 40:
        return "较高风险"
    else:
        return "高风险"


def _generate_risk_summary(risks: List[Dict[str, Any]]) -> str:
    """生成风险摘要文本。"""
    if not risks:
        return "未发现明显风险条款，合同整体较为安全。"

    high = [r for r in risks if r["severity"] == "high"]
    medium = [r for r in risks if r["severity"] == "medium"]

    parts = []
    if high:
        names = "、".join(r["category_name"] for r in high)
        parts.append(f"发现 {len(high)} 项高风险条款（{names}），建议重点关注")
    if medium:
        names = "、".join(r["category_name"] for r in medium)
        parts.append(f"发现 {len(medium)} 项中等风险条款（{names}），建议审慎评估")

    return "；".join(parts) + "。"


def main():
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        description="合同风险条款识别工具 — 基于关键词和模式匹配识别 12 类风险",
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=["analyze", "quick-check", "full-report"],
        help="操作类型: analyze（分析）, quick-check（快速检查）, full-report（完整报告）",
    )
    parser.add_argument(
        "--text",
        default=None,
        help="合同文本内容（直接传入）",
    )
    parser.add_argument(
        "--text-file",
        default=None,
        help="合同文本文件路径",
    )

    args = parser.parse_args()

    try:
        text = read_text_input(args)

        if args.action == "analyze":
            risks = analyze_risk(text)
            output_success({
                "total_risks": len(risks),
                "risks": risks,
            })

        elif args.action == "quick-check":
            result = quick_check(text)
            output_success(result)

        elif args.action == "full-report":
            result = full_report(text)
            output_success(result)

    except ValueError as e:
        output_error(str(e), "VALIDATION_ERROR")
    except FileNotFoundError as e:
        output_error(str(e), "FILE_NOT_FOUND")
    except Exception as e:
        output_error(f"分析失败: {e}", "ANALYSIS_ERROR")


if __name__ == "__main__":
    main()
