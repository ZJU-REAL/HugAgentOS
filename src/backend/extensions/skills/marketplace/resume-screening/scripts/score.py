#!/usr/bin/env python3
"""按评分标准汇总候选人得分，核验证据，输出排名表。

模型给每位候选人的每条标准打 0–5 分并附简历原文摘录（evidence）。
本脚本负责模型不可靠的部分：
  1. 证据核验：摘录必须能在简历原文里找到，找不到的那一项按 0 分计并标记；
  2. 统一加权：总分 = Σ(得分/5 × 权重) / Σ权重 × 100；
  3. 硬性条件：must_have 标准低于 min_score 直接判为“不符合”，不论总分多高；
  4. 输出 Excel（排名 + 逐项明细）和 Markdown 摘要。

用法：
    python score.py --rubric rubric.json --candidates candidates.jsonl \
        --scores scores.json -o ranking.xlsx [--mask-contact]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

VERIFIED = "已核验"
PUNCT_RE = re.compile(r"[\s　,，.。;；:：、!！?？\"'“”‘’()（）【】\[\]<>《》|/\\-]+")


def norm(s: str) -> str:
    return PUNCT_RE.sub("", s or "").lower()


def evidence_found(evidence: str, text_norm: str) -> bool:
    ev = norm(evidence)
    if len(ev) < 4:
        return False  # 太短的摘录（如“熟悉”）不能当证据
    if ev in text_norm:
        return True
    # 容忍模型改了个别字：最长公共片段覆盖摘录 85% 以上即认可
    m = SequenceMatcher(None, ev, text_norm, autojunk=False).find_longest_match(
        0, len(ev), 0, len(text_norm))
    return m.size >= 0.85 * len(ev)


def mask(value: str | None, kind: str) -> str | None:
    if not value:
        return value
    if kind == "phone" and len(value) >= 7:
        return value[:3] + "****" + value[-4:]
    if kind == "email" and "@" in value:
        user, domain = value.split("@", 1)
        return user[:1] + "***@" + domain
    return value


def load_jsonl(path: str) -> dict[str, dict]:
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            out[rec["candidate_id"]] = rec
    return out


def validate_rubric(rubric: dict) -> list[dict]:
    criteria = rubric.get("criteria") or []
    if not criteria:
        raise ValueError("rubric.criteria 不能为空")
    seen = set()
    for c in criteria:
        if not c.get("id") or c["id"] in seen:
            raise ValueError(f"标准 id 缺失或重复：{c.get('id')}")
        seen.add(c["id"])
        if float(c.get("weight", 0)) <= 0:
            raise ValueError(f"标准 {c['id']} 的权重必须大于 0")
    return criteria


def evaluate(rubric: dict, candidates: dict[str, dict], scores: list[dict]) -> list[dict]:
    criteria = validate_rubric(rubric)
    total_weight = sum(float(c["weight"]) for c in criteria)
    pass_line = float(rubric.get("pass_line", 60))
    results = []
    for entry in scores:
        cid = entry.get("candidate_id")
        cand = candidates.get(cid)
        if not cand:
            print(f"跳过：scores 里的 {cid} 不在 candidates 中", file=sys.stderr)
            continue
        text_norm = norm(cand["text"])
        given = {s.get("criterion"): s for s in entry.get("scores") or []}
        rows, flags, vetoed = [], [], []
        weighted = 0.0
        for c in criteria:
            s = given.get(c["id"])
            raw = s.get("score") if s else None
            evidence = (s or {}).get("evidence") or ""
            status = VERIFIED
            if raw is None:
                score, status = 0.0, "未评分"
            else:
                score = min(5.0, max(0.0, float(raw)))
                if score > 0 and not evidence_found(evidence, text_norm):
                    score, status = 0.0, "证据未在简历中找到"
            if status != VERIFIED:
                flags.append(f"{c.get('name', c['id'])}：{status}")
            if c.get("must_have") and score < float(c.get("min_score", 3)):
                vetoed.append(c.get("name", c["id"]))
            weighted += score / 5 * float(c["weight"])
            rows.append({"criterion": c.get("name", c["id"]), "weight": c["weight"],
                         "score": score, "evidence": evidence, "status": status})
        total = round(weighted / total_weight * 100, 1)
        if vetoed:
            verdict = "不符合（硬性条件：" + "、".join(vetoed) + "）"
        elif total >= pass_line:
            verdict = "建议面试"
        else:
            verdict = "待定"
        results.append({
            "candidate_id": cid, "name": entry.get("name") or cand["file"], "file": cand["file"],
            "phone": cand.get("phone"), "email": cand.get("email"),
            "highest_degree": cand.get("highest_degree"),
            "years_experience": cand.get("years_experience"),
            "total": total, "verdict": verdict, "flags": flags,
            "summary": entry.get("summary", ""), "rows": rows,
        })
    # 不符合的排在最后，其余按总分降序
    results.sort(key=lambda r: (r["verdict"].startswith("不符合"), -r["total"]))
    for i, r in enumerate(results, 1):
        r["rank"] = i
    return results


def write_xlsx(results: list[dict], path: str, mask_contact: bool) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "排名"
    head = ["排名", "姓名", "总分", "结论", "最高学历", "工作年限", "手机", "邮箱", "需人工复核", "评语", "文件"]
    ws.append(head)
    fills = {"建议面试": "E2F0D9", "待定": "FFF2CC"}
    for r in results:
        ws.append([
            r["rank"], r["name"], r["total"], r["verdict"], r["highest_degree"],
            r["years_experience"],
            mask(r["phone"], "phone") if mask_contact else r["phone"],
            mask(r["email"], "email") if mask_contact else r["email"],
            "；".join(r["flags"]), r["summary"], r["file"],
        ])
        color = fills.get(r["verdict"], "F4CCCC" if r["verdict"].startswith("不符合") else None)
        if color:
            ws.cell(ws.max_row, 4).fill = PatternFill("solid", fgColor=color)
    detail = wb.create_sheet("逐项明细")
    detail.append(["姓名", "标准", "权重", "得分", "状态", "简历证据"])
    for r in results:
        for row in r["rows"]:
            detail.append([r["name"], row["criterion"], row["weight"], row["score"],
                           row["status"], row["evidence"]])
    for sheet, widths in ((ws, [6, 12, 8, 26, 10, 10, 15, 24, 36, 48, 24]),
                          (detail, [12, 18, 8, 8, 18, 70])):
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for i, w in enumerate(widths, 1):
            sheet.column_dimensions[chr(64 + i)].width = w
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
        sheet.freeze_panes = "A2"
    wb.save(path)


def write_md(results: list[dict], rubric: dict, path: str) -> None:
    lines = [f"# {rubric.get('position', '岗位')} 简历筛选结果", "",
             f"共 {len(results)} 份；建议面试 {sum(r['verdict'] == '建议面试' for r in results)} 份。", "",
             "| 排名 | 姓名 | 总分 | 结论 | 需人工复核 |", "|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['rank']} | {r['name']} | {r['total']} | {r['verdict']} | "
                     f"{'；'.join(r['flags']) or '—'} |")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="简历评分汇总与排名")
    ap.add_argument("--rubric", required=True)
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--scores", required=True)
    ap.add_argument("-o", "--output", default="ranking.xlsx")
    ap.add_argument("--mask-contact", action="store_true", help="输出中隐藏手机号和邮箱中间部分")
    args = ap.parse_args()

    rubric = json.loads(Path(args.rubric).read_text(encoding="utf-8"))
    scores = json.loads(Path(args.scores).read_text(encoding="utf-8"))
    try:
        results = evaluate(rubric, load_jsonl(args.candidates), scores)
    except ValueError as exc:
        print(f"评分标准有误：{exc}", file=sys.stderr)
        return 2
    write_xlsx(results, args.output, args.mask_contact)
    md_path = str(Path(args.output).with_suffix(".md"))
    write_md(results, rubric, md_path)
    print(json.dumps({
        "ranked": len(results), "xlsx": args.output, "markdown": md_path,
        "flagged": [{"name": r["name"], "flags": r["flags"]} for r in results if r["flags"]],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
