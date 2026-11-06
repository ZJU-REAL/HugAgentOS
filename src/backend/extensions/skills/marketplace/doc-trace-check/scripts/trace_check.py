#!/usr/bin/env python3
"""公文 / 汇报材料依据溯源核验。

输入：
  - 草稿 Markdown：引用写成平台引用标记 `[锚文本](cite:e7)`，缺失信息写 `[待补]`；
  - 依据台账 JSON：每条依据一项 {cite_id, title, issuer, doc_no, date, url, quote}，
    quote 是检索结果里的原文摘录。

逐句检查：
  - 句子里有数字、金额、比例、文号、日期、《文件名》这类“硬信息”却没有引用 → 缺少依据；
  - 引用的 cite_id 不在台账里 → 引用无效；
  - 有引用，但句中的数字 / 文号在所引依据的原文摘录里找不到 → 与依据不符；
  - 句中带 [待补] → 待补（不算错误，但要提醒用户补齐）。

输出：溯源核验报告（.docx 或 .md）；可选 --render 生成带角标和依据清单的定稿 Markdown。

用法：
    python trace_check.py --draft 草稿.md --ledger 依据台账.json -o 溯源核验报告.docx \
        [--render 定稿.md]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

CITE_RE = re.compile(r"\[([^\]]*)\]\(cite:([A-Za-z0-9_-]+)\)")
PENDING_RE = re.compile(r"\[待补[^\]]*\]|【待补[^】]*】")
# 发改产业〔2024〕123号 / 国办发[2023]5号 / 京政发（2022）8号
DOC_NO_RE = re.compile(r"[一-龥]{1,12}[〔\[［(（]\s*(?:19|20)\d{2}\s*[〕\]］)）]\s*\d+\s*号")
DATE_RE = re.compile(r"(?:19|20)\d{2}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?)?")
TITLE_RE = re.compile(r"《[^》]{2,60}》")
# 带单位或百分号的数字；纯序号（第一、1.）不算。
# 前面只排除字母数字：Python 把汉字也算 \w，用 \w 会漏掉“企业512家”这种紧贴汉字的数字
NUM_RE = re.compile(
    r"(?<![0-9A-Za-z.])\d+(?:[.,]\d+)*\s*(?:%|％|个百分点|亿元|万元|元|亿|万|千|百|倍|家|个|项|人|户|台|套|吨|公里|千米|平方米|亩|兆瓦|GW|MW|千瓦时|年|个月)"
)
SENT_SPLIT_RE = re.compile(r"(?<=[。！？；!?;])|\n+")
STATUS_ORDER = ["引用无效", "与依据不符", "缺少依据", "待补", "已溯源"]


def norm_num(s: str) -> str:
    return re.sub(r"[\s,，]", "", s).replace("％", "%")


def hard_facts(sentence: str) -> dict[str, list[str]]:
    plain = CITE_RE.sub(lambda m: m.group(1), sentence)
    plain = PENDING_RE.sub("", plain)
    doc_nos = DOC_NO_RE.findall(plain)
    # 文号里的年份和号码不再重复算成“数字”
    rest = DOC_NO_RE.sub("", plain)
    dates = DATE_RE.findall(rest)
    rest = DATE_RE.sub("", rest)
    return {
        "doc_no": [norm_num(d) for d in doc_nos],
        "date": [norm_num(d) for d in dates],
        "title": TITLE_RE.findall(plain),
        "number": [norm_num(n) for n in NUM_RE.findall(rest)],
    }


def number_in(n: str, haystack: str) -> bool:
    """'3.5亿元' 只比对数字 3.5（单位写法允许不同），且必须是完整数字：512 不能匹配 1512。"""
    m = re.match(r"\d+(?:\.\d+)?", n)
    core = m.group(0) if m else n
    return re.search(rf"(?<![\d.]){re.escape(core)}(?![\d]|\.\d)", haystack) is not None


def load_ledger(path: str) -> dict[str, dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("items") or list(data.values())
    ledger = {}
    for item in data:
        cid = str(item.get("cite_id") or "").strip()
        if not cid:
            raise ValueError("台账中有条目缺少 cite_id")
        ledger[cid] = item
    return ledger


def source_text(item: dict) -> str:
    parts = [item.get(k) or "" for k in ("title", "doc_no", "date", "issuer", "quote")]
    return norm_num(" ".join(parts))


def split_sentences(markdown: str) -> list[str]:
    out = []
    for block in SENT_SPLIT_RE.split(markdown):
        s = block.strip()
        # 标题行、空行、分隔线不核验
        if not s or s.startswith("#") or set(s) <= set("-*_ |"):
            continue
        out.append(s)
    return out


def check(markdown: str, ledger: dict[str, dict]) -> list[dict]:
    rows = []
    for sent in split_sentences(markdown):
        cites = [cid for _, cid in CITE_RE.findall(sent)]
        facts = hard_facts(sent)
        has_facts = any(facts.values())
        pending = bool(PENDING_RE.search(sent))
        if not cites and not has_facts and not pending:
            continue
        problems = []
        missing_ids = [c for c in cites if c not in ledger]
        if missing_ids:
            status = "引用无效"
            problems.append("台账中没有：" + "、".join(missing_ids))
        elif cites:
            haystack = " ".join(source_text(ledger[c]) for c in cites)
            unmatched = [d for d in facts["doc_no"] if d not in haystack]
            unmatched += [t for t in facts["title"] if norm_num(t) not in haystack
                          and norm_num(t.strip("《》")) not in haystack]
            unmatched += [n for n in facts["number"] if not number_in(n, haystack)]
            if unmatched:
                status = "与依据不符"
                problems.append("所引依据中找不到：" + "、".join(dict.fromkeys(unmatched)))
            else:
                status = "待补" if pending else "已溯源"
        elif has_facts:
            status = "缺少依据"
            flat = [x for v in facts.values() for x in v]
            problems.append("未注明依据：" + "、".join(dict.fromkeys(flat)))
        else:
            status = "待补"
        if pending and status != "待补":
            problems.append("含待补信息")
        rows.append({
            "sentence": CITE_RE.sub(lambda m: m.group(1), sent),
            "status": status,
            "cites": cites,
            "sources": [ledger[c] for c in cites if c in ledger],
            "problems": problems,
        })
    return rows


def source_label(item: dict) -> str:
    bits = [item.get("issuer"), f"《{item['title'].strip('《》')}》" if item.get("title") else None,
            item.get("doc_no"), item.get("date")]
    return " ".join(b for b in bits if b)


def summary(rows: list[dict]) -> dict[str, int]:
    return {s: sum(r["status"] == s for r in rows) for s in STATUS_ORDER}


def write_md(rows: list[dict], path: str) -> None:
    counts = summary(rows)
    lines = ["# 依据溯源核验报告", "",
             "核验结果：" + "，".join(f"{k} {v} 处" for k, v in counts.items()), "",
             "| 序号 | 正文 | 状态 | 依据 | 问题 |", "|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        src = "<br>".join(source_label(s) for s in r["sources"]) or "—"
        cell = r["sentence"].replace("|", "\\|")
        lines.append(f"| {i} | {cell} | {r['status']} | {src} | {'；'.join(r['problems']) or '—'} |")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_docx(rows: list[dict], path: str) -> None:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    doc = Document()
    title = doc.add_heading("依据溯源核验报告", level=1)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    counts = summary(rows)
    doc.add_paragraph("核验结果：" + "，".join(f"{k} {v} 处" for k, v in counts.items()) + "。")
    table = doc.add_table(rows=1, cols=5)
    table.style = "Table Grid"
    for cell, text in zip(table.rows[0].cells, ["序号", "正文", "状态", "依据与原文摘录", "问题"]):
        cell.text = text
        cell.paragraphs[0].runs[0].bold = True
    colors = {"引用无效": "C00000", "与依据不符": "C00000", "缺少依据": "C55A11", "待补": "7F6000"}
    for i, r in enumerate(rows, 1):
        cells = table.add_row().cells
        cells[0].text = str(i)
        cells[1].text = r["sentence"]
        cells[2].text = r["status"]
        if r["status"] in colors:
            run = cells[2].paragraphs[0].runs[0]
            run.font.color.rgb = RGBColor.from_string(colors[r["status"]])
            run.bold = True
        cells[3].text = "\n".join(
            source_label(s) + (f"\n原文：{s['quote']}" if s.get("quote") else "")
            + (f"\n{s['url']}" if s.get("url") else "")
            for s in r["sources"]) or "—"
        cells[4].text = "；".join(r["problems"]) or "—"
        for c in cells:
            for p in c.paragraphs:
                for run in p.runs:
                    run.font.size = Pt(9)
    doc.save(path)


def render(markdown: str, ledger: dict[str, dict]) -> str:
    """把 cite 标记换成上标序号，文末附依据清单；台账里没有的引用原样保留锚文本。"""
    order: list[str] = []

    def repl(m: re.Match) -> str:
        anchor, cid = m.group(1), m.group(2)
        if cid not in ledger:
            return anchor
        if cid not in order:
            order.append(cid)
        return f"{anchor}<sup>[{order.index(cid) + 1}]</sup>"

    body = CITE_RE.sub(repl, markdown).rstrip()
    if not order:
        return body + "\n"
    refs = ["", "", "---", "", "**依据清单**", ""]
    for i, cid in enumerate(order, 1):
        item = ledger[cid]
        refs.append(f"[{i}] {source_label(item)}" + (f"，{item['url']}" if item.get("url") else ""))
    return body + "\n".join(refs) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="公文依据溯源核验")
    ap.add_argument("--draft", required=True, help="带 cite 标记的草稿 Markdown")
    ap.add_argument("--ledger", required=True, help="依据台账 JSON")
    ap.add_argument("-o", "--output", default="溯源核验报告.docx", help=".docx 或 .md")
    ap.add_argument("--render", help="另存一份带角标与依据清单的定稿 Markdown")
    args = ap.parse_args()

    markdown = Path(args.draft).read_text(encoding="utf-8")
    try:
        ledger = load_ledger(args.ledger)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"依据台账有误：{exc}", file=sys.stderr)
        return 2
    rows = check(markdown, ledger)
    if args.output.lower().endswith(".docx"):
        write_docx(rows, args.output)
    else:
        write_md(rows, args.output)
    if args.render:
        Path(args.render).write_text(render(markdown, ledger), encoding="utf-8")
    issues = [{"sentence": r["sentence"][:60], "status": r["status"], "problems": r["problems"]}
              for r in rows if r["status"] != "已溯源"]
    print(json.dumps({"summary": summary(rows), "report": args.output, "rendered": args.render,
                      "issues": issues}, ensure_ascii=False, indent=2))
    # 有硬错误时返回 1，方便模型据此决定是否需要回改
    return 1 if any(r["status"] in {"引用无效", "与依据不符", "缺少依据"} for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
