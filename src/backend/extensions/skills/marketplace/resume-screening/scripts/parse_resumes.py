#!/usr/bin/env python3
"""批量解析简历：把 PDF / DOCX / TXT / MD 简历抽成统一的 JSONL，供模型逐份评分。

只做确定性的事：读全文、用正则抽手机号 / 邮箱 / 最高学历 / 工作年限。
姓名、项目经历这类需要理解的字段留给模型从 text 里读，不在这里猜。

用法：
    python parse_resumes.py <文件或目录> [...] -o candidates.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

SUPPORTED = {".pdf", ".docx", ".txt", ".md"}

PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d[\s-]?\d{4}[\s-]?\d{4})(?!\d)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 从高到低，命中第一个即为最高学历
DEGREES = [
    ("博士", re.compile(r"博士|Ph\.?D|Doctor", re.I)),
    ("硕士", re.compile(r"硕士|研究生|Master|MBA|M\.S\.|MSc", re.I)),
    ("本科", re.compile(r"本科|学士|Bachelor|B\.S\.|BSc|B\.A\.", re.I)),
    ("大专", re.compile(r"大专|专科|高职|Associate", re.I)),
]
# “5年经验”“3 年 Python 开发经验”“10+年相关经验”；年和经验之间允许夹一小段技能名
YEARS_RE = re.compile(r"(?<!\d)(\d{1,2})\s*(?:\+|多)?\s*年(?:以上)?[^\d\n，。,;；]{0,16}?经验")
# 含这些词的行是教育经历，时间段不算工作年限
EDU_LINE_RE = re.compile(r"大学|学院|学校|本科|硕士|博士|研究生|大专|专科|学历|教育|毕业|University|College|School", re.I)
# 2018.03 - 2021.06 / 2018年3月-至今 / 2019/07 – present
RANGE_RE = re.compile(
    r"((?:19|20)\d{2})\s*[./年-]\s*(\d{1,2})?\s*月?\s*[-–—~至到]+\s*"
    r"(至今|现在|今|present|now|(?:19|20)\d{2})\s*[./年-]?\s*(\d{1,2})?",
    re.I,
)


def read_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        for enc in ("utf-8", "gb18030"):
            try:
                return path.read_text(encoding=enc)
            except UnicodeDecodeError:
                continue
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".docx":
        from docx import Document

        doc = Document(str(path))
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text.strip() for cell in row.cells))
        return "\n".join(parts)
    if suffix == ".pdf":
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    raise ValueError(f"不支持的格式：{suffix}")


def months_between(y1: int, m1: int, y2: int, m2: int) -> int:
    return max(0, (y2 - y1) * 12 + (m2 - m1))


def estimate_years(text: str) -> float | None:
    """优先用简历自述的“N 年经验”；没有就把工作经历的时间段合并后估算。"""
    stated = [int(m) for m in YEARS_RE.findall(text) if 0 < int(m) < 50]
    if stated:
        return float(max(stated))
    today = date.today()
    spans = []
    work_lines = "\n".join(line for line in text.splitlines() if not EDU_LINE_RE.search(line))
    for y1, m1, end, m2 in RANGE_RE.findall(work_lines):
        start = (int(y1), int(m1 or 1))
        if end.lower() in {"至今", "现在", "今", "present", "now"}:
            stop = (today.year, today.month)
        else:
            stop = (int(end), int(m2 or 12))
        if stop >= start:
            spans.append((start, stop))
    if not spans:
        return None
    # 合并重叠区间，避免教育经历和工作经历重叠时重复计算
    spans.sort()
    merged = [list(spans[0])]
    for start, stop in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], stop)
        else:
            merged.append([start, stop])
    total = sum(months_between(*a, *b) for a, b in merged)
    return round(total / 12, 1)


def highest_degree(text: str) -> str | None:
    for name, pattern in DEGREES:
        if pattern.search(text):
            return name
    return None


def parse_one(path: Path) -> dict:
    text = read_text(path)
    clean = re.sub(r"[ \t　]+", " ", text).strip()
    phones = sorted({re.sub(r"[\s-]", "", p) for p in PHONE_RE.findall(clean)})
    return {
        "candidate_id": hashlib.sha1(path.name.encode("utf-8")).hexdigest()[:8],
        "file": path.name,
        "chars": len(clean),
        "phone": phones[0] if phones else None,
        "email": (EMAIL_RE.findall(clean) or [None])[0],
        "highest_degree": highest_degree(clean),
        "years_experience": estimate_years(clean),
        # 少于 50 字基本是扫描件或图片 PDF，需要先 OCR
        "needs_ocr": len(clean) < 50,
        "text": clean,
    }


def collect(inputs: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in inputs:
        p = Path(raw)
        if p.is_dir():
            files.extend(sorted(f for f in p.rglob("*") if f.suffix.lower() in SUPPORTED))
        elif p.is_file() and p.suffix.lower() in SUPPORTED:
            files.append(p)
        else:
            print(f"跳过：{raw}（不存在或格式不支持）", file=sys.stderr)
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description="批量解析简历为 JSONL")
    ap.add_argument("inputs", nargs="+", help="简历文件或目录")
    ap.add_argument("-o", "--output", default="candidates.jsonl")
    args = ap.parse_args()

    files = collect(args.inputs)
    if not files:
        print("没有找到可解析的简历（支持 .pdf .docx .txt .md）", file=sys.stderr)
        return 1
    ok = failed = ocr = 0
    with open(args.output, "w", encoding="utf-8") as out:
        for f in files:
            try:
                rec = parse_one(f)
            except Exception as exc:  # noqa: BLE001 - 单份失败不影响整批
                print(f"解析失败：{f.name}（{exc}）", file=sys.stderr)
                failed += 1
                continue
            ocr += rec["needs_ocr"]
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            ok += 1
    print(json.dumps({"parsed": ok, "failed": failed, "needs_ocr": ocr, "output": args.output},
                     ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
