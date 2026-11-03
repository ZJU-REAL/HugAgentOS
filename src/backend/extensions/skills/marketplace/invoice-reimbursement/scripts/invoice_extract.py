#!/usr/bin/env python3
"""从电子发票中抽取报销所需字段。

支持：带文字层的 PDF 电子发票（全电发票、增值税电子普通 / 专用发票），以及 OCR 后保存的 .txt。
图片和扫描件没有文字层，需要先用 OCR 工具识别成文字存为 .txt 再传进来。

每张发票输出一条记录，抽不到的字段列在 missing 里，同时附原文 text，
由模型对照原文补齐或更正后再交给 reimburse_check.py。

用法：
    python invoice_extract.py <文件或目录> [...] -o invoices.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SUPPORTED = {".pdf", ".txt"}
REQUIRED = ["invoice_no", "issue_date", "buyer_name", "buyer_tax_id", "seller_name",
            "amount", "tax", "total"]

# 统一社会信用代码（18 位）；老税号 15 / 20 位也放行
TAX_ID_RE = re.compile(r"(?<![0-9A-Z])([0-9A-HJ-NPQRTUWXY]{2}\d{6}[0-9A-HJ-NPQRTUWXY]{10}|\d{15}|\d{20})(?![0-9A-Z])")
LABELED_TAX_ID_RE = re.compile(r"识别号\s*[:：]\s*([0-9A-Z]{15,20})(?![0-9A-Z])")
NAME_RE =re.compile(r"名\s*称\s*[:：]\s*([^\s:：]{2,60}?)(?=\s|销|售|购|买|统一|纳税|$)", re.M)
MONEY = r"[¥￥]?\s*(-?\d[\d,]*\.\d{2})"


def read_text(path: Path) -> str:
    if path.suffix.lower() == ".txt":
        for enc in ("utf-8", "gb18030"):
            try:
                return path.read_text(encoding=enc)
            except UnicodeDecodeError:
                continue
    import pdfplumber

    with pdfplumber.open(str(path)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def money(value: str | None) -> float | None:
    return round(float(value.replace(",", "")), 2) if value else None


def first(pattern: str, text: str, flags: int = 0) -> str | None:
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None


def invoice_kind(text: str) -> str:
    if "专用发票" in text:
        return "增值税专用发票"
    if "普通发票" in text:
        return "普通发票"
    return "未知"


def extract(text: str) -> dict:
    flat = re.sub(r"[ \t　]+", " ", text)
    rec: dict = {"kind": invoice_kind(flat)}
    rec["invoice_code"] = first(r"发票代码\s*[:：]?\s*(\d{10,12})", flat)
    # 全电发票号码 20 位；老版电子发票 8 位
    rec["invoice_no"] = first(r"发票号码\s*[:：]?\s*(\d{20}|\d{8})(?!\d)", flat)
    m = re.search(r"开票日期\s*[:：]?\s*((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", flat)
    rec["issue_date"] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None
    rec["check_code"] = first(r"校\s*验\s*码\s*[:：]?\s*([\d\s]{20,30})", flat)
    if rec["check_code"]:
        rec["check_code"] = rec["check_code"].replace(" ", "")

    # 版式里购买方总在销售方前面：按出现顺序取第一个、第二个
    names = [n for n in NAME_RE.findall(flat) if not n.startswith(("统一", "纳税"))]
    rec["buyer_name"] = names[0] if names else None
    rec["seller_name"] = names[1] if len(names) > 1 else None
    tax_ids = TAX_ID_RE.findall(flat.replace(rec.get("invoice_no") or "#", " "))
    if len(tax_ids) < 2:
        # 严格格式没取全时（OCR 认错字符、非标税号），退回按“识别号”标签取值
        tax_ids = LABELED_TAX_ID_RE.findall(flat) or tax_ids
    rec["buyer_tax_id"] = tax_ids[0] if tax_ids else None
    rec["seller_tax_id"] = tax_ids[1] if len(tax_ids) > 1 else None

    totals = re.search(r"合\s*计\s*" + MONEY + r"\s*" + MONEY, flat)
    rec["amount"] = money(totals.group(1)) if totals else None
    rec["tax"] = money(totals.group(2)) if totals else None
    rec["total"] = money(first(r"[(（]\s*小\s*写\s*[)）]\s*" + MONEY, flat))
    if rec["total"] is None:
        rec["total"] = money(first(r"价税合计[^\d¥￥]{0,30}" + MONEY, flat))
    # 免税发票的税额常写作“***”，此时合计行只有一个金额
    if rec["amount"] is None:
        single = re.search(r"合\s*计\s*" + MONEY, flat)
        if single:
            rec["amount"], rec["tax"] = money(single.group(1)), 0.0

    # 项目行形如“*餐饮服务*餐费”，取星号里的税收分类作为费用类别
    items = re.findall(r"\*([^*\s]{2,20})\*([^\s]{0,30})", flat)
    rec["items"] = [f"*{c}*{n}" for c, n in items]
    rec["category"] = items[0][0] if items else None
    rec["missing"] = [k for k in REQUIRED if rec.get(k) in (None, "")]
    return rec


def collect(inputs: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in inputs:
        p = Path(raw)
        if p.is_dir():
            files.extend(sorted(f for f in p.rglob("*") if f.suffix.lower() in SUPPORTED))
        elif p.is_file() and p.suffix.lower() in SUPPORTED:
            files.append(p)
        else:
            print(f"跳过：{raw}（不存在，或是图片需要先 OCR 成 .txt）", file=sys.stderr)
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description="电子发票字段抽取")
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-o", "--output", default="invoices.json")
    args = ap.parse_args()

    files = collect(args.inputs)
    if not files:
        print("没有可处理的发票（支持带文字层的 .pdf 和 OCR 后的 .txt）", file=sys.stderr)
        return 1
    records = []
    for f in files:
        try:
            text = read_text(f)
        except Exception as exc:  # noqa: BLE001 - 单张失败不影响整批
            print(f"读取失败：{f.name}（{exc}）", file=sys.stderr)
            continue
        if len(text.strip()) < 30:
            print(f"{f.name} 没有文字层，请先 OCR", file=sys.stderr)
            continue
        rec = extract(text)
        rec["file"] = f.name
        rec["text"] = text
        records.append(rec)
    Path(args.output).write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "extracted": len(records), "output": args.output,
        "incomplete": [{"file": r["file"], "missing": r["missing"]} for r in records if r["missing"]],
    }, ensure_ascii=False, indent=2))
    return 0 if records else 1


if __name__ == "__main__":
    sys.exit(main())
