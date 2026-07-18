#!/usr/bin/env python3
"""
contract-guardian 共享工具模块

提供订阅校验、JSON 输入输出、文件读取、敏感信息脱敏等通用功能。
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional


# ============================================================
# 常量
# ============================================================

DATA_DIR = os.environ.get(
    "CG_DATA_DIR",
    os.path.join(os.path.expanduser("~"), ".openclaw-bdi", "contract-guardian"),
)


# ============================================================
# 订阅校验
# ============================================================

_SUBSCRIPTION_TIERS: Dict[str, Dict[str, Any]] = {
    "free": {
        "tier": "free",
        "daily_review_limit": 1,
        "risk_categories": 3,
        "expiry_tracking_limit": 3,
        "contract_compare": False,
        "template_count": 3,
        "supported_formats": ["txt", "md"],
        "history_search": False,
        "features": [
            "key_info_extract",
            "basic_risk_check",
            "expiry_reminder",
            "basic_templates",
        ],
    },
    "paid": {
        "tier": "paid",
        "daily_review_limit": -1,  # -1 表示无限制
        "risk_categories": 12,
        "expiry_tracking_limit": -1,
        "contract_compare": True,
        "template_count": 20,
        "supported_formats": ["txt", "md", "pdf", "docx"],
        "history_search": True,
        "features": [
            "key_info_extract",
            "full_risk_check",
            "expiry_reminder",
            "contract_compare",
            "all_templates",
            "history_search",
            "multi_format",
        ],
    },
}


def check_subscription(tier: Optional[str] = None) -> Dict[str, Any]:
    """检查当前订阅等级并返回对应的限制配置。

    优先使用传入的 tier 参数，否则读取环境变量 CG_SUBSCRIPTION_TIER。
    若都未设置，默认为 "free" 等级。

    Args:
        tier: 订阅等级（"free" 或 "paid"），可选。

    Returns:
        包含订阅限制信息的字典。

    Raises:
        ValueError: 当传入的等级无效时抛出。
    """
    if tier is None:
        tier = os.environ.get("CG_SUBSCRIPTION_TIER", "free")

    tier = tier.strip().lower()

    if tier not in _SUBSCRIPTION_TIERS:
        valid = ", ".join(_SUBSCRIPTION_TIERS.keys())
        raise ValueError(f"无效的订阅等级: {tier!r}，有效等级: {valid}")

    return dict(_SUBSCRIPTION_TIERS[tier])


def is_paid() -> bool:
    """快速判断当前是否为付费版。"""
    tier = os.environ.get("CG_SUBSCRIPTION_TIER", "free").strip().lower()
    return tier == "paid"


def check_feature(feature: str) -> bool:
    """检查当前订阅是否包含指定功能。

    Args:
        feature: 功能标识符。

    Returns:
        True 表示当前订阅包含该功能。
    """
    sub = check_subscription()
    return feature in sub.get("features", [])


# ============================================================
# JSON 输入输出
# ============================================================

def read_json_stdin() -> Dict[str, Any]:
    """从标准输入读取 JSON 数据并解析为字典。

    Returns:
        解析后的字典对象。

    Raises:
        ValueError: 当输入为空或 JSON 格式不合法时抛出。
    """
    try:
        raw = sys.stdin.read()
    except Exception as e:
        raise ValueError(f"读取标准输入失败: {e}")

    if not raw.strip():
        raise ValueError("标准输入为空，未读取到任何数据")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败: {e}")

    if not isinstance(data, dict):
        raise ValueError(f"期望输入为 JSON 对象，实际类型为 {type(data).__name__}")

    return data


def output_json(data: Any) -> None:
    """将数据以 JSON 格式输出到标准输出。

    Args:
        data: 待输出的数据（可被 JSON 序列化的任意对象）。
    """
    print(json.dumps(data, ensure_ascii=False, default=str))


def output_error(message: str, code: str = "ERROR") -> None:
    """输出标准错误响应到标准输出。

    Args:
        message: 错误描述信息。
        code: 错误代码，默认为 "ERROR"。
    """
    result = {
        "success": False,
        "error": {
            "code": code,
            "message": message,
        },
    }
    output_json(result)


def output_success(data: Any) -> None:
    """输出标准成功响应到标准输出。

    Args:
        data: 成功时返回的数据负载。
    """
    result = {
        "success": True,
        "data": data,
    }
    output_json(result)


# ============================================================
# 文件读取
# ============================================================

def read_text_file(file_path: str) -> str:
    """读取文本文件内容。

    Args:
        file_path: 文件路径。

    Returns:
        文件文本内容。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 文件编码错误。
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"文件不存在: {file_path}")

    for encoding in ("utf-8", "gbk", "gb2312", "latin-1"):
        try:
            with open(file_path, "r", encoding=encoding) as f:
                return f.read()
        except UnicodeDecodeError:
            continue

    raise ValueError(f"无法识别文件编码: {file_path}")


def read_text_input(args: argparse.Namespace) -> str:
    """从命令行参数中获取文本内容。

    支持 --text 直接传入文本或 --text-file 传入文件路径。

    Args:
        args: 包含 text 和 text_file 属性的命名空间。

    Returns:
        文本内容。

    Raises:
        ValueError: 未提供文本输入。
    """
    if hasattr(args, "text") and args.text:
        return args.text
    if hasattr(args, "text_file") and args.text_file:
        return read_text_file(args.text_file)
    raise ValueError("请通过 --text 或 --text-file 提供文本内容")


# ============================================================
# 敏感信息脱敏
# ============================================================

def mask_sensitive_info(text: str) -> str:
    """对文本中的敏感信息进行脱敏处理。

    脱敏规则:
    - 身份证号: 保留前3后4，中间用 * 替代
    - 手机号: 保留前3后4，中间用 * 替代
    - 银行卡号: 保留前4后4，中间用 * 替代

    Args:
        text: 待脱敏的文本。

    Returns:
        脱敏后的文本。
    """
    # 身份证号 (18位)
    text = re.sub(
        r'\b(\d{3})\d{11}(\d{4})\b',
        r'\1***********\2',
        text,
    )
    # 手机号 (11位，以1开头)
    text = re.sub(
        r'\b(1\d{2})\d{4}(\d{4})\b',
        r'\1****\2',
        text,
    )
    # 银行卡号 (16-19位)
    text = re.sub(
        r'\b(\d{4})\d{8,11}(\d{4})\b',
        r'\1********\2',
        text,
    )
    return text


# ============================================================
# 数据目录管理
# ============================================================

def ensure_data_dir() -> str:
    """确保数据目录存在并返回路径。

    Returns:
        数据目录的绝对路径。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    return DATA_DIR


def get_data_file(filename: str) -> str:
    """获取数据目录中文件的完整路径。

    Args:
        filename: 文件名。

    Returns:
        文件的完整路径。
    """
    ensure_data_dir()
    return os.path.join(DATA_DIR, filename)


# ============================================================
# 日期工具
# ============================================================

def parse_date(date_str: str) -> Optional[datetime]:
    """尝试解析常见格式的日期字符串。

    支持格式: YYYY-MM-DD, YYYY/MM/DD, YYYY年MM月DD日, YYYYMMDD

    Args:
        date_str: 日期字符串。

    Returns:
        解析成功返回 datetime 对象，失败返回 None。
    """
    date_str = date_str.strip()
    formats = [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y年%m月%d日",
        "%Y%m%d",
        "%Y.%m.%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


def format_currency(value: float) -> str:
    """将金额格式化为中文货币表示。

    Args:
        value: 金额数值。

    Returns:
        格式化后的金额字符串，例如 "¥1,234,567.00"。
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)

    if abs(num) >= 1e8:
        return f"¥{num / 1e8:.2f}亿"
    elif abs(num) >= 1e4:
        return f"¥{num / 1e4:.2f}万"
    else:
        return f"¥{num:,.2f}"
