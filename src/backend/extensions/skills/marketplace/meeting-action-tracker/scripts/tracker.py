#!/usr/bin/env python3
"""例会待办台账：跨会议跟踪待办事项的状态。

台账是一个 JSON 文件（一个系列例会一份），记录每条待办的负责人、截止日期、状态和每次会议上的进展。
模型负责从纪要里提取待办、在会上收集进展；本脚本负责编号、状态流转、逾期判断和导出，
保证跨会议的数据不丢、不重复、不被随手改写。

子命令：
  init        新建台账
  add-meeting 登记一次会议及其新增待办（items JSON）
  update      登记已有待办的进展（updates JSON）
  open        列出未关闭待办（含逾期标记），供下次会议回顾
  export      导出 Excel 台账 + 下次会议回顾清单（Markdown）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

STATUSES = ["未开始", "进行中", "已完成", "延期", "取消"]
CLOSED = {"已完成", "取消"}


def parse_date(value: str | None, field: str) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError:
        raise ValueError(f"{field} 日期格式应为 YYYY-MM-DD，收到：{value}") from None


def load(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"台账不存在：{path}，请先运行 init")
    return json.loads(p.read_text(encoding="utf-8"))


def save(ledger: dict, path: str) -> None:
    tmp = Path(path).with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # 先写临时文件再替换，避免写一半时损坏台账


def read_json_arg(value: str):
    p = Path(value)
    text = p.read_text(encoding="utf-8") if p.is_file() else value
    return json.loads(text)


def is_overdue(item: dict, today: str) -> bool:
    return item["status"] not in CLOSED and bool(item.get("due")) and item["due"] < today


def cmd_init(args) -> dict:
    if Path(args.ledger).exists():
        raise ValueError(f"台账已存在：{args.ledger}")
    ledger = {"series": args.series, "meetings": [], "items": []}
    save(ledger, args.ledger)
    return {"created": args.ledger, "series": args.series}


def cmd_add_meeting(args) -> dict:
    ledger = load(args.ledger)
    mdate = parse_date(args.date, "会议日期")
    mid = f"M{len(ledger['meetings']) + 1:03d}"
    ledger["meetings"].append({"id": mid, "date": mdate, "title": args.title or mdate})
    added, warnings = [], []
    existing = {(i["title"].strip(), i.get("owner")) for i in ledger["items"] if i["status"] not in CLOSED}
    for raw in read_json_arg(args.items) if args.items else []:
        title = (raw.get("title") or "").strip()
        if not title:
            warnings.append("跳过一条没有内容的待办")
            continue
        owner = (raw.get("owner") or "").strip() or "待定"
        if (title, owner) in existing:
            warnings.append(f"重复待办已跳过（仍未关闭）：{title}")
            continue
        iid = f"A{len(ledger['items']) + 1:03d}"
        item = {"id": iid, "title": title, "owner": owner,
                "due": parse_date(raw.get("due"), f"待办「{title}」截止日期"),
                "status": "未开始", "source_meeting": mid,
                "history": [{"meeting": mid, "date": mdate, "status": "未开始",
                             "note": raw.get("note", "")}]}
        if owner == "待定":
            warnings.append(f"{iid} 没有负责人：{title}")
        if not item["due"]:
            warnings.append(f"{iid} 没有截止日期：{title}")
        ledger["items"].append(item)
        existing.add((title, owner))
        added.append(iid)
    save(ledger, args.ledger)
    return {"meeting": mid, "added": added, "warnings": warnings}


def cmd_update(args) -> dict:
    ledger = load(args.ledger)
    meeting = next((m for m in ledger["meetings"] if m["id"] == args.meeting), None)
    if not meeting:
        raise ValueError(f"会议 {args.meeting} 不存在；先用 add-meeting 登记本次会议")
    by_id = {i["id"]: i for i in ledger["items"]}
    changed, warnings = [], []
    for upd in read_json_arg(args.updates):
        item = by_id.get(upd.get("id"))
        if not item:
            warnings.append(f"未找到待办：{upd.get('id')}")
            continue
        status = upd.get("status") or item["status"]
        if status not in STATUSES:
            warnings.append(f"{item['id']} 状态无效：{status}（可选：{'/'.join(STATUSES)}）")
            continue
        if item["status"] in CLOSED and status not in CLOSED:
            warnings.append(f"{item['id']} 已关闭后重新打开")
        new_due = parse_date(upd.get("due"), f"{item['id']} 截止日期") or item.get("due")
        if status == "延期" and new_due == item.get("due"):
            warnings.append(f"{item['id']} 标记为延期但没有给出新的截止日期")
        if upd.get("owner"):
            item["owner"] = upd["owner"]
        item["status"], item["due"] = status, new_due
        item["history"].append({"meeting": meeting["id"], "date": meeting["date"],
                                "status": status, "note": upd.get("note", "")})
        changed.append(item["id"])
    save(ledger, args.ledger)
    return {"updated": changed, "warnings": warnings}


def open_items(ledger: dict, today: str) -> list[dict]:
    items = [i for i in ledger["items"] if i["status"] not in CLOSED]
    # 逾期的排最前，其次按截止日期，没有截止日期的放最后
    items.sort(key=lambda i: (not is_overdue(i, today), i.get("due") or "9999-12-31", i["id"]))
    return [dict(i, overdue=is_overdue(i, today),
                 last_note=next((h["note"] for h in reversed(i["history"]) if h.get("note")), ""))
            for i in items]


def cmd_open(args) -> dict:
    ledger = load(args.ledger)
    today = parse_date(args.as_of, "as-of") or date.today().isoformat()
    rows = open_items(ledger, today)
    return {"as_of": today, "open": len(rows), "overdue": sum(r["overdue"] for r in rows),
            "items": [{k: r[k] for k in ("id", "title", "owner", "due", "status", "overdue", "last_note")}
                      for r in rows]}


def cmd_export(args) -> dict:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    ledger = load(args.ledger)
    today = parse_date(args.as_of, "as-of") or date.today().isoformat()
    meetings = {m["id"]: m for m in ledger["meetings"]}
    wb = Workbook()
    ws = wb.active
    ws.title = "待办台账"
    ws.append(["编号", "事项", "负责人", "截止日期", "状态", "是否逾期", "来源会议", "最新进展"])
    red = PatternFill("solid", fgColor="F4CCCC")
    green = PatternFill("solid", fgColor="E2F0D9")
    for i in ledger["items"]:
        last = i["history"][-1]
        src = meetings.get(i["source_meeting"], {})
        ws.append([i["id"], i["title"], i["owner"], i.get("due") or "", i["status"],
                   "是" if is_overdue(i, today) else "", src.get("title", i["source_meeting"]),
                   last.get("note", "")])
        if is_overdue(i, today):
            ws.cell(ws.max_row, 6).fill = red
        elif i["status"] == "已完成":
            ws.cell(ws.max_row, 5).fill = green
    hist = wb.create_sheet("进展记录")
    hist.append(["编号", "事项", "会议", "日期", "状态", "说明"])
    for i in ledger["items"]:
        for h in i["history"]:
            hist.append([i["id"], i["title"], meetings.get(h["meeting"], {}).get("title", h["meeting"]),
                         h["date"], h["status"], h.get("note", "")])
    for sheet, widths in ((ws, [8, 40, 10, 12, 8, 8, 18, 40]), (hist, [8, 40, 18, 12, 8, 40])):
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for idx, w in enumerate(widths, 1):
            sheet.column_dimensions[chr(64 + idx)].width = w
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
        sheet.freeze_panes = "A2"
    wb.save(args.output)

    rows = open_items(ledger, today)
    agenda = [f"# {ledger['series']} 待办回顾（截至 {today}）", "",
              f"未关闭 {len(rows)} 项，其中逾期 {sum(r['overdue'] for r in rows)} 项。", ""]
    owners: dict[str, list[dict]] = {}
    for r in rows:
        owners.setdefault(r["owner"], []).append(r)
    for owner, items in owners.items():
        agenda.append(f"## {owner}")
        for r in items:
            flag = "【逾期】" if r["overdue"] else ""
            due = f"，截止 {r['due']}" if r.get("due") else "，未定截止日期"
            note = f"；上次进展：{r['last_note']}" if r["last_note"] else ""
            agenda.append(f"- {flag}{r['id']} {r['title']}（{r['status']}{due}{note}）")
        agenda.append("")
    md_path = str(Path(args.output).with_suffix(".md"))
    Path(md_path).write_text("\n".join(agenda), encoding="utf-8")
    return {"xlsx": args.output, "agenda": md_path, "open": len(rows)}


def main() -> int:
    ap = argparse.ArgumentParser(description="例会待办台账")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--ledger", required=True)
    p.add_argument("--series", required=True, help="例会名称，如“产品部周例会”")
    p = sub.add_parser("add-meeting")
    p.add_argument("--ledger", required=True)
    p.add_argument("--date", required=True, help="会议日期 YYYY-MM-DD")
    p.add_argument("--title")
    p.add_argument("--items", help="新增待办 JSON 文件或字符串：[{title, owner, due, note}]")
    p = sub.add_parser("update")
    p.add_argument("--ledger", required=True)
    p.add_argument("--meeting", required=True, help="本次会议编号（add-meeting 返回的 M00x）")
    p.add_argument("--updates", required=True, help="进展 JSON：[{id, status, note, due, owner}]")
    for name in ("open", "export"):
        p = sub.add_parser(name)
        p.add_argument("--ledger", required=True)
        p.add_argument("--as-of", help="按哪天判断逾期，默认今天")
        if name == "export":
            p.add_argument("-o", "--output", default="待办台账.xlsx")
    args = ap.parse_args()
    handler = {"init": cmd_init, "add-meeting": cmd_add_meeting, "update": cmd_update,
               "open": cmd_open, "export": cmd_export}[args.cmd]
    try:
        result = handler(args)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
