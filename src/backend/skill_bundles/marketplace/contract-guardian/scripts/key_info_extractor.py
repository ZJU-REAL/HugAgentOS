#!/usr/bin/env python3
"""
contract-guardian 关键信息提取模块

使用正则表达式从合同文本中提取关键信息，包括甲乙方、金额、期限、违约金等。
"""

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import (
    format_currency,
    output_error,
    output_success,
    parse_date,
    read_text_input,
)


# ============================================================
# 提取模式定义
# ============================================================

def extract_party_a(text: str) -> Optional[str]:
    """提取甲方信息。"""
    patterns = [
        r"甲\s*方[：:]\s*(.+?)(?:\n|$|（|签章|地址|联系)",
        r"发包方[：:]\s*(.+?)(?:\n|$|（)",
        r"委托方[：:]\s*(.+?)(?:\n|$|（)",
        r"买方[：:]\s*(.+?)(?:\n|$|（)",
        r"需方[：:]\s*(.+?)(?:\n|$|（)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    return None


def extract_party_b(text: str) -> Optional[str]:
    """提取乙方信息。"""
    patterns = [
        r"乙\s*方[：:]\s*(.+?)(?:\n|$|（|签章|地址|联系)",
        r"承包方[：:]\s*(.+?)(?:\n|$|（)",
        r"受托方[：:]\s*(.+?)(?:\n|$|（)",
        r"卖方[：:]\s*(.+?)(?:\n|$|（)",
        r"供方[：:]\s*(.+?)(?:\n|$|（)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    return None


def extract_contract_amount(text: str) -> Optional[Dict[str, Any]]:
    """提取合同金额。"""
    patterns = [
        # ¥ 或 元 格式
        r"(?:合同|总|合计|项目).*?(?:金额|价[格款]|费用|总[价额])[：:为]?\s*(?:人民币)?\s*[¥￥]?\s*([\d,，]+(?:\.\d+)?)\s*(?:元|万元|万)",
        r"[¥￥]\s*([\d,，]+(?:\.\d+)?)\s*(?:元|万元)?",
        r"(?:人民币)\s*([\d,，]+(?:\.\d+)?)\s*(?:元|万元|万)",
        r"(?:金额|价[格款]|费用|总[价额])[：:为]?\s*([\d,，]+(?:\.\d+)?)\s*(?:元|万元|万)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            amount_str = match.group(1).replace(",", "").replace("，", "")
            try:
                amount = float(amount_str)
                # 检查是否为万元
                context = text[max(0, match.start() - 10):match.end() + 10]
                if "万元" in context or "万" in context:
                    amount *= 10000
                return {
                    "raw": match.group(0).strip(),
                    "amount": amount,
                    "formatted": format_currency(amount),
                }
            except ValueError:
                continue
    return None


def extract_dates(text: str) -> Dict[str, Optional[str]]:
    """提取合同日期信息（起止日期、签订日期）。"""
    result = {
        "start_date": None,
        "end_date": None,
        "signing_date": None,
    }

    # 合同期限 / 起止日期
    period_patterns = [
        r"(?:自|从)\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?)\s*(?:起|开始)?\s*(?:至|到|—|－|-)\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?)",
        r"(?:期限|有效期)[：:为]?\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?)\s*(?:至|到|—|－|-)\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?)",
    ]
    for pattern in period_patterns:
        match = re.search(pattern, text)
        if match:
            start = parse_date(match.group(1))
            end = parse_date(match.group(2))
            if start:
                result["start_date"] = start.strftime("%Y-%m-%d")
            if end:
                result["end_date"] = end.strftime("%Y-%m-%d")
            break

    # 签订日期
    sign_patterns = [
        r"(?:签订|签署|签章).*?日期[：:]\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?)",
        r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?)\s*(?:签订|签署|签章)",
        r"(?:本合同|本协议).*?于\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?)\s*(?:签订|签署|生效)",
    ]
    for pattern in sign_patterns:
        match = re.search(pattern, text)
        if match:
            dt = parse_date(match.group(1))
            if dt:
                result["signing_date"] = dt.strftime("%Y-%m-%d")
            break

    return result


def extract_penalty_clause(text: str) -> Optional[str]:
    """提取违约金条款。"""
    patterns = [
        r"(?:违约金|违约责任|违约赔偿)[：:，,]?\s*(.{10,200}?)(?:\n|。|；)",
        r"(?:逾期|迟延|延迟).*?(?:违约金|滞纳金)[：:，,]?\s*(.{10,150}?)(?:\n|。|；)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0).strip()
    return None


def extract_payment_terms(text: str) -> Optional[str]:
    """提取付款条件。"""
    patterns = [
        r"(?:付款|支付|结算)[方条]?[式件][：:，,]?\s*(.{10,300}?)(?:\n\n|\n[一二三四五六七八九十])",
        r"(?:付款|支付|结算).*?(?:方式|条件|期限)[：:]\s*(.{10,200}?)(?:\n|。)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(0).strip()[:300]
    return None


def extract_contact_info(text: str) -> Dict[str, Optional[str]]:
    """提取联系方式信息。"""
    result = {
        "phone": None,
        "email": None,
        "address": None,
    }

    # 电话
    phone_match = re.search(r"(?:电话|联系电话|手机|Tel)[：:]\s*([\d\-+() ]{7,20})", text)
    if phone_match:
        result["phone"] = phone_match.group(1).strip()

    # 邮箱
    email_match = re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text)
    if email_match:
        result["email"] = email_match.group(0)

    # 地址
    addr_patterns = [
        r"(?:地址|住所|注册地址)[：:]\s*(.{5,100}?)(?:\n|$|电话|邮编|联系)",
    ]
    for pattern in addr_patterns:
        match = re.search(pattern, text)
        if match:
            result["address"] = match.group(1).strip()
            break

    return result


def extract_key_info(text: str) -> Dict[str, Any]:
    """从合同文本中提取全部关键信息。

    Args:
        text: 合同文本。

    Returns:
        包含所有提取信息的字典。
    """
    dates = extract_dates(text)
    contact = extract_contact_info(text)
    amount = extract_contract_amount(text)

    return {
        "party_a": extract_party_a(text),
        "party_b": extract_party_b(text),
        "contract_amount": amount,
        "start_date": dates["start_date"],
        "end_date": dates["end_date"],
        "signing_date": dates["signing_date"],
        "penalty_clause": extract_penalty_clause(text),
        "payment_terms": extract_payment_terms(text),
        "contact_info": contact,
    }


def generate_summary(info: Dict[str, Any]) -> str:
    """根据提取的关键信息生成摘要文本。

    Args:
        info: extract_key_info 返回的字典。

    Returns:
        Markdown 格式的摘要文本。
    """
    lines = ["## 合同关键信息摘要\n"]

    party_a = info.get("party_a") or "未识别"
    party_b = info.get("party_b") or "未识别"
    lines.append(f"| 项目 | 内容 |")
    lines.append(f"|------|------|")
    lines.append(f"| 甲方 | {party_a} |")
    lines.append(f"| 乙方 | {party_b} |")

    amount = info.get("contract_amount")
    if amount:
        lines.append(f"| 合同金额 | {amount['formatted']} |")
    else:
        lines.append(f"| 合同金额 | 未识别 |")

    start = info.get("start_date") or "未识别"
    end = info.get("end_date") or "未识别"
    signing = info.get("signing_date") or "未识别"
    lines.append(f"| 合同期限 | {start} 至 {end} |")
    lines.append(f"| 签订日期 | {signing} |")

    penalty = info.get("penalty_clause")
    if penalty:
        # 截断过长内容
        display = penalty[:80] + "..." if len(penalty) > 80 else penalty
        lines.append(f"| 违约条款 | {display} |")

    payment = info.get("payment_terms")
    if payment:
        display = payment[:80] + "..." if len(payment) > 80 else payment
        lines.append(f"| 付款条件 | {display} |")

    return "\n".join(lines)


def main():
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        description="合同关键信息提取工具 — 提取甲乙方、金额、期限等关键信息",
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=["extract", "summary"],
        help="操作类型: extract（提取信息）, summary（生成摘要）",
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
        info = extract_key_info(text)

        if args.action == "extract":
            output_success(info)

        elif args.action == "summary":
            summary = generate_summary(info)
            output_success({
                "info": info,
                "summary": summary,
            })

    except ValueError as e:
        output_error(str(e), "VALIDATION_ERROR")
    except FileNotFoundError as e:
        output_error(str(e), "FILE_NOT_FOUND")
    except Exception as e:
        output_error(f"提取失败: {e}", "EXTRACTION_ERROR")


if __name__ == "__main__":
    main()
