#!/usr/bin/env python3
"""
contract-guardian 合同存档与到期提醒模块

支持合同元数据的存档、检索和到期提醒功能。
免费版最多追踪 3 份合同到期，付费版无限制。
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import (
    check_subscription,
    ensure_data_dir,
    get_data_file,
    output_error,
    output_success,
)


STORE_FILE = "contracts.json"


def _load_store() -> List[Dict[str, Any]]:
    """加载合同存档数据。"""
    store_path = get_data_file(STORE_FILE)
    if not os.path.exists(store_path):
        return []
    try:
        with open(store_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, IOError):
        return []


def _save_store(contracts: List[Dict[str, Any]]) -> None:
    """保存合同存档数据。"""
    store_path = get_data_file(STORE_FILE)
    ensure_data_dir()
    with open(store_path, "w", encoding="utf-8") as f:
        json.dump(contracts, f, ensure_ascii=False, indent=2, default=str)


def _generate_id(contracts: List[Dict[str, Any]]) -> str:
    """生成合同 ID。"""
    if not contracts:
        return "CG-001"
    max_id = 0
    for c in contracts:
        cid = c.get("id", "CG-000")
        try:
            num = int(cid.split("-")[1])
            max_id = max(max_id, num)
        except (IndexError, ValueError):
            continue
    return f"CG-{max_id + 1:03d}"


def archive_contract(data: Dict[str, Any]) -> Dict[str, Any]:
    """存档一份合同。

    Args:
        data: 合同元数据，应包含:
            - title: 合同标题
            - party_a: 甲方
            - party_b: 乙方
            - start_date: 开始日期
            - end_date: 结束日期
            - amount: 合同金额（可选）
            - file_path: 合同文件路径（可选）
            - notes: 备注（可选）

    Returns:
        存档结果，包含生成的合同 ID。
    """
    contracts = _load_store()
    sub = check_subscription()

    # 检查存档数量限制
    limit = sub.get("expiry_tracking_limit", 3)
    if limit != -1 and len(contracts) >= limit:
        raise ValueError(
            f"当前免费版最多存档 {limit} 份合同。"
            f"已存档 {len(contracts)} 份，无法继续添加。"
            f"升级至付费版（¥129/月）可无限存档。"
        )

    contract_id = _generate_id(contracts)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    record = {
        "id": contract_id,
        "title": data.get("title", "未命名合同"),
        "party_a": data.get("party_a"),
        "party_b": data.get("party_b"),
        "start_date": data.get("start_date"),
        "end_date": data.get("end_date"),
        "amount": data.get("amount"),
        "file_path": data.get("file_path"),
        "notes": data.get("notes"),
        "archived_at": now,
        "status": "active",
    }

    contracts.append(record)
    _save_store(contracts)

    return {
        "id": contract_id,
        "message": f"合同「{record['title']}」已成功存档",
        "record": record,
    }


def list_contracts(status: str = None) -> Dict[str, Any]:
    """列出所有存档合同。

    Args:
        status: 筛选状态（active/expired/all），默认 all。

    Returns:
        合同列表和统计信息。
    """
    contracts = _load_store()

    # 更新过期状态
    today = datetime.now().strftime("%Y-%m-%d")
    for c in contracts:
        end_date = c.get("end_date")
        if end_date and end_date < today and c.get("status") == "active":
            c["status"] = "expired"
    _save_store(contracts)

    # 筛选
    if status and status != "all":
        filtered = [c for c in contracts if c.get("status") == status]
    else:
        filtered = contracts

    return {
        "total": len(filtered),
        "active": sum(1 for c in contracts if c.get("status") == "active"),
        "expired": sum(1 for c in contracts if c.get("status") == "expired"),
        "contracts": filtered,
    }


def search_contracts(keyword: str) -> Dict[str, Any]:
    """搜索合同。

    Args:
        keyword: 搜索关键词，在标题、甲乙方、备注中匹配。

    Returns:
        搜索结果。
    """
    sub = check_subscription()
    if not sub.get("history_search"):
        raise ValueError(
            "历史合同检索为付费功能。升级至付费版（¥129/月）即可使用。"
        )

    contracts = _load_store()
    keyword_lower = keyword.lower()

    matched = []
    for c in contracts:
        searchable = " ".join(
            str(v) for v in [
                c.get("title", ""),
                c.get("party_a", ""),
                c.get("party_b", ""),
                c.get("notes", ""),
            ]
        ).lower()
        if keyword_lower in searchable:
            matched.append(c)

    return {
        "keyword": keyword,
        "total": len(matched),
        "contracts": matched,
    }


def get_expiring_contracts(days: int = 30) -> Dict[str, Any]:
    """获取即将到期的合同。

    Args:
        days: 到期天数阈值，默认 30 天。

    Returns:
        到期合同清单，按到期日排序。
    """
    contracts = _load_store()
    today = datetime.now()

    expiring_30 = []
    expiring_60 = []
    expiring_90 = []

    for c in contracts:
        if c.get("status") != "active":
            continue
        end_date_str = c.get("end_date")
        if not end_date_str:
            continue

        try:
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
        except ValueError:
            continue

        remaining = (end_date - today).days

        if remaining < 0:
            continue  # 已过期
        elif remaining <= 30:
            expiring_30.append({**c, "remaining_days": remaining})
        elif remaining <= 60:
            expiring_60.append({**c, "remaining_days": remaining})
        elif remaining <= 90:
            expiring_90.append({**c, "remaining_days": remaining})

    # 按剩余天数排序
    expiring_30.sort(key=lambda x: x["remaining_days"])
    expiring_60.sort(key=lambda x: x["remaining_days"])
    expiring_90.sort(key=lambda x: x["remaining_days"])

    return {
        "check_date": today.strftime("%Y-%m-%d"),
        "expiring_30_days": {
            "count": len(expiring_30),
            "contracts": expiring_30,
        },
        "expiring_60_days": {
            "count": len(expiring_60),
            "contracts": expiring_60,
        },
        "expiring_90_days": {
            "count": len(expiring_90),
            "contracts": expiring_90,
        },
        "total_expiring": len(expiring_30) + len(expiring_60) + len(expiring_90),
    }


def main():
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        description="合同存档与到期提醒工具 — 管理合同存档，追踪到期日",
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=["archive", "list", "search", "expiring"],
        help="操作类型: archive（存档）, list（列表）, search（搜索）, expiring（到期提醒）",
    )
    parser.add_argument(
        "--data",
        default=None,
        help="合同数据（JSON 格式字符串）",
    )
    parser.add_argument(
        "--data-file",
        default=None,
        help="合同数据文件路径（JSON 文件）",
    )
    parser.add_argument(
        "--keyword",
        default=None,
        help="搜索关键词（用于 search 操作）",
    )
    parser.add_argument(
        "--status",
        default="all",
        choices=["active", "expired", "all"],
        help="合同状态筛选（用于 list 操作）",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="到期天数阈值（用于 expiring 操作）",
    )

    args = parser.parse_args()

    try:
        if args.action == "archive":
            data = None
            if args.data:
                data = json.loads(args.data)
            elif args.data_file:
                with open(args.data_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                raise ValueError("请通过 --data 或 --data-file 提供合同数据")

            result = archive_contract(data)
            output_success(result)

        elif args.action == "list":
            result = list_contracts(args.status)
            output_success(result)

        elif args.action == "search":
            if not args.keyword:
                raise ValueError("请通过 --keyword 提供搜索关键词")
            result = search_contracts(args.keyword)
            output_success(result)

        elif args.action == "expiring":
            result = get_expiring_contracts(args.days)
            output_success(result)

    except json.JSONDecodeError as e:
        output_error(f"JSON 格式错误: {e}", "JSON_ERROR")
    except ValueError as e:
        output_error(str(e), "VALIDATION_ERROR")
    except FileNotFoundError as e:
        output_error(str(e), "FILE_NOT_FOUND")
    except Exception as e:
        output_error(f"操作失败: {e}", "STORE_ERROR")


if __name__ == "__main__":
    main()
