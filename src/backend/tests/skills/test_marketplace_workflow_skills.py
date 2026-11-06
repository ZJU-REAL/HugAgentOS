"""End-to-end checks for the script-backed marketplace skills.

Each skill keeps the deterministic half of its workflow in scripts (evidence checks,
ledgers, rule checks); these tests run those scripts as the sandbox would and assert
the decisions they exist to make. Inputs are plain text so only openpyxl is needed;
the .docx / .pdf readers are exercised in the sandbox image.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from extensions.paths import SKILLS_DIR

MARKET = SKILLS_DIR / "marketplace"
SKILLS = ["resume-screening", "doc-trace-check", "meeting-action-tracker", "invoice-reimbursement"]


def _run(script: Path, *args: str, cwd: Path, ok_codes=(0,)) -> dict:
    proc = subprocess.run([sys.executable, str(script), *args], cwd=cwd,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode in ok_codes, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("slug", SKILLS)
def test_skill_ships_manifest_and_frontmatter(slug):
    from core.capabilities.skills.registry import split_frontmatter
    from features.marketplace import skill_metadata, skills_state

    manifest = skill_metadata.read_manifest(slug)
    assert manifest and manifest["entry_name"] == slug
    assert manifest["category"] in skills_state.MARKETPLACE_CATEGORIES
    meta, body = split_frontmatter((MARKET / slug / "SKILL.md").read_text(encoding="utf-8"))
    assert meta["name"] == slug and meta["description"] and "{baseDir}/scripts/" in body


def test_resume_scores_reject_evidence_missing_from_the_resume(tmp_path):
    pytest.importorskip("openpyxl")
    scripts = MARKET / "resume-screening/scripts"
    (tmp_path / "in").mkdir()
    (tmp_path / "in/a.txt").write_text(
        "李四 13912345678\n2012.09-2016.06 某大学 本科\n"
        "2016.07 - 2019.06 甲公司 Java 开发\n2019.07 - 至今 乙科技 用 Go 重构订单服务\n",
        encoding="utf-8")
    (tmp_path / "in/b.txt").write_text("王五 硕士\n3年Python开发经验，熟悉 PostgreSQL\n", encoding="utf-8")
    parsed = _run(scripts / "parse_resumes.py", "in", "-o", "c.jsonl", cwd=tmp_path)
    assert parsed["parsed"] == 2
    cands = {json.loads(l)["file"]: json.loads(l) for l in (tmp_path / "c.jsonl").read_text("utf-8").splitlines()}
    assert cands["b.txt"]["years_experience"] == 3.0
    # Study years are not work years: only the two jobs (2016.07–2019.06, 2019.07–now) count.
    today = date.today()
    months = (2019 - 2016) * 12 + (6 - 7) + (today.year - 2019) * 12 + (today.month - 7)
    assert cands["a.txt"]["years_experience"] == round(months / 12, 1)

    (tmp_path / "rubric.json").write_text(json.dumps({"position": "后端", "pass_line": 60, "criteria": [
        {"id": "lang", "name": "后端", "weight": 60, "must_have": True, "min_score": 3},
        {"id": "db", "name": "数据库", "weight": 40}]}), encoding="utf-8")
    (tmp_path / "scores.json").write_text(json.dumps([
        {"candidate_id": cands["a.txt"]["candidate_id"], "name": "李四", "scores": [
            {"criterion": "lang", "score": 4, "evidence": "用 Go 重构订单服务"},
            {"criterion": "db", "score": 2, "evidence": "2016.07 - 2019.06 甲公司 Java 开发"}]},
        {"candidate_id": cands["b.txt"]["candidate_id"], "name": "王五", "scores": [
            {"criterion": "lang", "score": 5, "evidence": "主导千万级支付系统架构"},
            {"criterion": "db", "score": 3, "evidence": "熟悉 PostgreSQL"}]}]), encoding="utf-8")
    out = _run(scripts / "score.py", "--rubric", "rubric.json", "--candidates", "c.jsonl",
               "--scores", "scores.json", "-o", "r.xlsx", "--mask-contact", cwd=tmp_path)
    assert out["flagged"] == [{"name": "王五", "flags": ["后端：证据未在简历中找到"]}]
    md = (tmp_path / "r.md").read_text(encoding="utf-8")
    assert "| 1 | 李四 | 64.0 | 建议面试 |" in md
    assert "王五 | 24.0 | 不符合（硬性条件：后端）" in md


def test_trace_check_flags_each_kind_of_unsupported_claim(tmp_path):
    script = MARKET / "doc-trace-check/scripts/trace_check.py"
    (tmp_path / "d.md").write_text(
        "# 报告\n\n增加值增长[6.8%](cite:e1)。\n全市建成5G基站3.2万个。\n"
        "认定专精特新企业[512家](cite:e2)。\n资金[待补]亿元。\n见[某文](cite:e9)。\n我们将抓好落实。\n",
        encoding="utf-8")
    (tmp_path / "l.json").write_text(json.dumps([
        {"cite_id": "e1", "title": "统计公报", "quote": "规模以上工业增加值增长6.8%。"},
        {"cite_id": "e2", "title": "工作总结", "quote": "累计认定专精特新企业1512家。"}]), encoding="utf-8")
    out = _run(script, "--draft", "d.md", "--ledger", "l.json", "-o", "r.md", "--render", "f.md",
               cwd=tmp_path, ok_codes=(1,))
    assert out["summary"] == {"引用无效": 1, "与依据不符": 1, "缺少依据": 1, "待补": 1, "已溯源": 1}
    # 512 must not be accepted just because the source says 1512.
    assert any(i["status"] == "与依据不符" and "512" in i["sentence"] for i in out["issues"])
    final = (tmp_path / "f.md").read_text(encoding="utf-8")
    assert "6.8%<sup>[1]</sup>" in final and "[1] 《统计公报》" in final and "cite:" not in final


def test_meeting_tracker_carries_items_across_meetings(tmp_path):
    pytest.importorskip("openpyxl")
    script = MARKET / "meeting-action-tracker/scripts/tracker.py"
    run = lambda *a, **k: _run(script, *a, cwd=tmp_path, **k)  # noqa: E731
    run("init", "--ledger", "l.json", "--series", "周例会")
    first = run("add-meeting", "--ledger", "l.json", "--date", "2026-10-27", "--items", json.dumps([
        {"title": "需求评审", "owner": "张三", "due": "2026-10-30"},
        {"title": "竞品报告", "owner": "李四", "due": "2026-11-10"},
        {"title": "排查告警"}]))
    assert first["added"] == ["A001", "A002", "A003"]
    assert "A003 没有负责人：排查告警" in first["warnings"]
    second = run("add-meeting", "--ledger", "l.json", "--date", "2026-11-03",
                 "--items", json.dumps([{"title": "竞品报告", "owner": "李四"}]))
    assert second["added"] == [] and second["warnings"] == ["重复待办已跳过（仍未关闭）：竞品报告"]
    upd = run("update", "--ledger", "l.json", "--meeting", "M002", "--updates", json.dumps([
        {"id": "A001", "status": "已完成"}, {"id": "A002", "status": "延期", "note": "数据未到"},
        {"id": "A003", "status": "进行中", "owner": "赵六", "due": "2026-11-01"}]))
    assert upd["warnings"] == ["A002 标记为延期但没有给出新的截止日期"]
    opened = run("open", "--ledger", "l.json", "--as-of", "2026-11-03")
    assert [(i["id"], i["overdue"]) for i in opened["items"]] == [("A003", True), ("A002", False)]
    run("export", "--ledger", "l.json", "--as-of", "2026-11-03", "-o", "t.xlsx")
    agenda = (tmp_path / "t.md").read_text(encoding="utf-8")
    assert "【逾期】A003 排查告警" in agenda and "上次进展：数据未到" in agenda
    bad = subprocess.run([sys.executable, str(script), "update", "--ledger", "l.json",
                          "--meeting", "M009", "--updates", "[]"], cwd=tmp_path, capture_output=True, text=True)
    assert bad.returncode == 2 and "M009 不存在" in bad.stderr


INVOICE = """电子发票（普通发票）
发票号码：{no}
开票日期：{date}
购 名称：{buyer} 销 名称：北京某某餐饮管理有限公司
买 统一社会信用代码/纳税人识别号：91110108MA01ABCD2X 售 统一社会信用代码/纳税人识别号：91110105MA02EFGH3Y
*餐饮服务*餐费 1 {amount} {amount} 6% {tax}
合 计 ¥{amount} ¥{tax}
价税合计（大写） 略 （小写）¥{total}
"""


def test_reimbursement_check_applies_company_rules(tmp_path):
    pytest.importorskip("openpyxl")
    scripts = MARKET / "invoice-reimbursement/scripts"
    (tmp_path / "in").mkdir()
    cases = {
        "1.txt": dict(no="24442000000000000001", date="2026年10月15日", buyer="某某科技有限公司",
                      amount="471.70", tax="28.30", total="500.00"),
        "2.txt": dict(no="24442000000000000001", date="2026年10月15日", buyer="某某科技有限公司",
                      amount="471.70", tax="28.30", total="500.00"),
        "3.txt": dict(no="24442000000000000003", date="2026年06月01日", buyer="别的公司",
                      amount="600.00", tax="36.00", total="638.00"),
        "4.txt": dict(no="24442000000000000004", date="2026年10月20日", buyer="某某科技有限公司",
                      amount="800.00", tax="48.00", total="848.00"),
    }
    for name, fields in cases.items():
        (tmp_path / "in" / name).write_text(INVOICE.format(**fields), encoding="utf-8")
    ext = _run(scripts / "invoice_extract.py", "in", "-o", "inv.json", cwd=tmp_path)
    assert ext["extracted"] == 4 and ext["incomplete"] == []
    first = json.loads((tmp_path / "inv.json").read_text("utf-8"))[0]
    assert (first["issue_date"], first["buyer_tax_id"], first["amount"], first["tax"], first["total"]) == (
        "2026-10-15", "91110108MA01ABCD2X", 471.7, 28.3, 500.0)
    (tmp_path / "p.json").write_text(json.dumps({
        "company_name": "某某科技有限公司", "max_age_days": 90, "category_limits": {"餐饮": 1000},
        "category_keywords": {"餐饮": ["餐饮"]}}), encoding="utf-8")
    out = _run(scripts / "reimburse_check.py", "--invoices", "inv.json", "--policy", "p.json",
               "--as-of", "2026-11-03", "-o", "s.xlsx", cwd=tmp_path)
    issues = {p["file"]: p["issues"] for p in out["problems"]}
    assert issues["2.txt"] == ["重复报销：与本批 1.txt 发票号码相同"]
    assert issues["3.txt"] == ["抬头不符：别的公司", "金额不平：600.0 + 36.0 ≠ 638.0", "超过报销时限（90 天）"]
    assert issues["4.txt"] == ["超标：餐饮累计 1348.0 元，限额 1000 元"]
    assert (out["passed"], out["reimbursable_amount"]) == (1, 500.0)
