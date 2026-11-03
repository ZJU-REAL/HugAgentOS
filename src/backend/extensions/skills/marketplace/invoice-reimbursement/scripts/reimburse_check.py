#!/usr/bin/env python3
"""按报销规则检查一批发票，生成报销汇总表和发票查验清单。

检查项（都是确定性的规则，不依赖模型判断）：
  - 重复报销：同一批内或历史已报销记录里出现过同一发票号码；
  - 抬头不符：购买方名称或税号与公司信息不一致；
  - 金额不平：金额 + 税额 ≠ 价税合计（允许 0.01 误差）；
  - 超期：开票日期早于报销时限；
  - 超标：按费用类别累计超过限额；
  - 关键字段缺失。

真伪查验需要在国家税务总局查验平台输入验证码，只能由用户本人操作；
本脚本只生成查验所需字段的清单。

用法：
    python reimburse_check.py --invoices invoices.json --policy policy.json \
        [--history reimbursed.txt] [--as-of 2026-11-03] -o 报销汇总.xlsx
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

VERIFY_URL = "https://inv-veri.chinatax.gov.cn"
REQUIRED = {"invoice_no": "发票号码", "issue_date": "开票日期", "buyer_name": "购买方名称",
            "total": "价税合计"}


def norm_name(s: str | None) -> str:
    return (s or "").replace("（", "(").replace("）", ")").replace(" ", "").strip()


def load_history(path: str | None) -> set[str]:
    if not path or not Path(path).is_file():
        return set()
    return {line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()}


def category_of(inv: dict, mapping: dict[str, list[str]]) -> str:
    """把发票的税收分类（如“餐饮服务”）归到报销类别（如“餐饮”）；归不上的记为“其他”。"""
    probe = " ".join([inv.get("category") or ""] + list(inv.get("items") or []))
    for cat, keywords in mapping.items():
        if any(k in probe for k in keywords):
            return cat
    return "其他"


def check(invoices: list[dict], policy: dict, history: set[str], today: date) -> list[dict]:
    company = norm_name(policy.get("company_name"))
    tax_id = (policy.get("tax_id") or "").strip().upper()
    max_age = int(policy.get("max_age_days") or 0)
    limits = policy.get("category_limits") or {}
    mapping = policy.get("category_keywords") or {}
    seen: dict[str, str] = {}
    spent: dict[str, float] = {}
    rows = []
    for inv in invoices:
        issues = []
        for key, label in REQUIRED.items():
            if inv.get(key) in (None, ""):
                issues.append(f"缺少{label}")
        no = (inv.get("invoice_no") or "").strip()
        key = f"{inv.get('invoice_code') or ''}{no}"
        if no:
            if key in seen:
                issues.append(f"重复报销：与本批 {seen[key]} 发票号码相同")
            elif no in history or key in history:
                issues.append("重复报销：历史已报销")
            seen.setdefault(key, inv.get("file", no))
        if company and inv.get("buyer_name") and norm_name(inv["buyer_name"]) != company:
            issues.append(f"抬头不符：{inv['buyer_name']}")
        if tax_id and inv.get("buyer_tax_id") and inv["buyer_tax_id"].upper() != tax_id:
            issues.append(f"税号不符：{inv['buyer_tax_id']}")
        amount, tax, total = inv.get("amount"), inv.get("tax"), inv.get("total")
        if None not in (amount, tax, total) and abs(amount + tax - total) > 0.01:
            issues.append(f"金额不平：{amount} + {tax} ≠ {total}")
        if max_age and inv.get("issue_date"):
            try:
                issued = date.fromisoformat(inv["issue_date"])
            except ValueError:
                issues.append(f"开票日期格式错误：{inv['issue_date']}")
            else:
                if issued > today:
                    issues.append("开票日期晚于今天")
                elif today - issued > timedelta(days=max_age):
                    issues.append(f"超过报销时限（{max_age} 天）")
        cat = category_of(inv, mapping)
        # 有问题的发票不计入类别累计，避免一张作废票把同类别的正常发票挤成超标
        if total is not None and not issues:
            spent[cat] = round(spent.get(cat, 0.0) + total, 2)
            limit = limits.get(cat)
            if limit is not None and spent[cat] > float(limit):
                issues.append(f"超标：{cat}累计 {spent[cat]} 元，限额 {limit} 元")
        rows.append({**{k: v for k, v in inv.items() if k != "text"},
                     "reimburse_category": cat, "issues": issues,
                     "result": "通过" if not issues else "需处理"})
    return rows


def write_xlsx(rows: list[dict], path: str) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "报销明细"
    ws.append(["序号", "结论", "问题", "报销类别", "开票日期", "发票号码", "销售方", "项目",
               "金额", "税额", "价税合计", "发票类型", "文件"])
    for i, r in enumerate(rows, 1):
        ws.append([i, r["result"], "；".join(r["issues"]), r["reimburse_category"], r.get("issue_date"),
                   r.get("invoice_no"), r.get("seller_name"), "、".join(r.get("items") or []),
                   r.get("amount"), r.get("tax"), r.get("total"), r.get("kind"), r.get("file")])
        fill = "E2F0D9" if r["result"] == "通过" else "F4CCCC"
        ws.cell(ws.max_row, 2).fill = PatternFill("solid", fgColor=fill)
    summary = wb.create_sheet("分类汇总")
    summary.append(["报销类别", "张数", "价税合计", "其中可报销"])
    cats: dict[str, list[float]] = {}
    for r in rows:
        c = cats.setdefault(r["reimburse_category"], [0, 0.0, 0.0])
        c[0] += 1
        c[1] += r.get("total") or 0
        if r["result"] == "通过":
            c[2] += r.get("total") or 0
    for cat, (n, total, ok) in cats.items():
        summary.append([cat, n, round(total, 2), round(ok, 2)])
    summary.append(["合计", sum(v[0] for v in cats.values()),
                    round(sum(v[1] for v in cats.values()), 2), round(sum(v[2] for v in cats.values()), 2)])
    verify = wb.create_sheet("查验清单")
    verify.append(["发票代码", "发票号码", "开票日期", "开具金额(不含税)", "校验码后6位", "价税合计", "查验结果"])
    for r in rows:
        code = r.get("check_code") or ""
        verify.append([r.get("invoice_code") or "", r.get("invoice_no"), r.get("issue_date"),
                       r.get("amount"), code[-6:], r.get("total"), ""])
    verify.append([])
    verify.append([f"请在国家税务总局全国增值税发票查验平台 {VERIFY_URL} 逐张查验后填写“查验结果”列"])
    for sheet in wb.worksheets:
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for col in sheet.columns:
            sheet.column_dimensions[col[0].column_letter].width = 16
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
        sheet.freeze_panes = "A2"
    ws.column_dimensions["C"].width = 40
    wb.save(path)


def main() -> int:
    ap = argparse.ArgumentParser(description="发票报销规则检查")
    ap.add_argument("--invoices", required=True, help="invoice_extract.py 的输出（可经模型更正）")
    ap.add_argument("--policy", required=True, help="报销规则 JSON")
    ap.add_argument("--history", help="历史已报销发票号码，每行一个")
    ap.add_argument("--as-of", help="按哪天判断超期，默认今天")
    ap.add_argument("-o", "--output", default="报销汇总.xlsx")
    args = ap.parse_args()

    invoices = json.loads(Path(args.invoices).read_text(encoding="utf-8"))
    policy = json.loads(Path(args.policy).read_text(encoding="utf-8"))
    try:
        today = date.fromisoformat(args.as_of) if args.as_of else date.today()
    except ValueError:
        print(f"--as-of 日期格式应为 YYYY-MM-DD：{args.as_of}", file=sys.stderr)
        return 2
    rows = check(invoices, policy, load_history(args.history), today)
    write_xlsx(rows, args.output)
    passed = [r for r in rows if r["result"] == "通过"]
    print(json.dumps({
        "total": len(rows), "passed": len(passed),
        "reimbursable_amount": round(sum(r.get("total") or 0 for r in passed), 2),
        "output": args.output,
        "problems": [{"file": r.get("file"), "invoice_no": r.get("invoice_no"), "issues": r["issues"]}
                     for r in rows if r["issues"]],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
