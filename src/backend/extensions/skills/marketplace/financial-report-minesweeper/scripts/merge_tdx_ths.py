#!/usr/bin/env python3
"""
财报排雷 v1.3.0 — TDX+THS 数据合并脚本
将 THS 的 abstract / dividend / pledge_ratio / IS缺失字段 合并入 TDX JSON

用法：
    python merge_tdx_ths.py <tdx_json> <ths_json>
"""

import json
import os
import sys

# THS IS 中 TDX 可能缺失的字段
THS_IS_SUPPLEMENT = [
    "其中：利息费用",
    "其中：利息收入",
    "营业税金及附加",
]


def main():
    if len(sys.argv) < 3:
        print("用法: python merge_tdx_ths.py <tdx_json> <ths_json>")
        sys.exit(1)

    with open(sys.argv[1], "r", encoding="utf-8") as f:
        tdx = json.load(f)
    with open(sys.argv[2], "r", encoding="utf-8") as f:
        ths = json.load(f)

    # 补充 abstract（THS 财务摘要更完整）
    if ths.get("abstract"):
        tdx["abstract"] = ths["abstract"]

    # 补充 dividend
    if ths.get("dividend"):
        tdx["dividend"] = ths["dividend"]

    # 补充 pledge_ratio
    if ths.get("pledge_ratio") is not None:
        tdx["pledge_ratio"] = ths["pledge_ratio"]

    # 补充 IS 中 TDX 缺失的字段
    ths_pl = ths.get("profit_statement", {})
    tdx_pl = tdx.get("profit_statement", {})
    for field in THS_IS_SUPPLEMENT:
        if field in ths_pl and field not in tdx_pl:
            tdx_pl[field] = ths_pl[field]

    tdx["profit_statement"] = tdx_pl

    # 安全写入：先写临时文件，成功后再替换（防止 json.dump 崩溃时截断原文件）
    import tempfile
    import shutil
    tmp_fd, tmp_path = tempfile.mkstemp(suffix='.json', text=True)
    try:
        with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
            json.dump(tdx, f, ensure_ascii=False, indent=2)
        shutil.move(tmp_path, sys.argv[1])
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    print(f"✓ 合并完成：TDX BS/IS/CF + THS abstract({len(tdx.get('abstract',{}))}期)/dividend({len(tdx.get('dividend',[]))}条)")


if __name__ == "__main__":
    main()
