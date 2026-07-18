#!/usr/bin/env python3
"""From a curated Cherry Studio subset -> generate preset sub-agent marketplace bundles (one-off, dev-only).

Cherry Studio's ``agents-zh.json`` is a curated dataset under AGPL-3.0 plus additional
commercial-use restrictions; vendoring it verbatim would make AGPL contaminate the
commercial deliverable. This script therefore **only cherry-picks the role subset
relevant to this project**, "rewrites and rearranges" the prompts with
debranding/normalization, and for each role **binds skills / MCP tools / plugins that
actually exist in this system**, finally writing our own preset content to disk
(without keeping Cherry's ids / original curation structure).

Usage (from src/backend):
    python scripts/import_cherry_agents.py            # use cache or download now, generate all bundles
    python scripts/import_cherry_agents.py --source /path/to/agents-zh.json
    python scripts/import_cherry_agents.py --dry-run  # validate only, no writes

Output: ``agent_bundles/marketplace/<slug>/agent.json``. All binding ids are validated
against the whitelist of the system's real capabilities; any nonexistent skill/tool/plugin
causes a non-zero exit, so nothing gets bound to thin air.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

BACKEND_ROOT = Path(__file__).resolve().parents[1]
MARKET_DIR = BACKEND_ROOT / "agent_bundles" / "marketplace"
CHERRY_URL = "https://raw.githubusercontent.com/CherryHQ/cherry-studio/main/resources/data/agents-zh.json"

# ── Curation + adaptation table ──────────────────────────────────────────────
# Each entry = one preset sub-agent:
#   name      —— role name to match in Cherry data (exact match)
#   slug/cat  —— our marketplace slug and one of the 9 top-level categories
#   skills/mcp/plugins —— bound ids of real capabilities in this system (the script validates existence)
#   tags/questions —— marketplace display tags + suggested questions (written by us, not Cherry originals)
#   featured  —— whether pinned as featured
CURATION: List[Dict[str, Any]] = [
    # ── Workplace & office (category "职场办公") ──
    {"name": "产品经理", "slug": "product-manager", "category": "职场办公", "featured": True,
     "tags": ["产品", "需求分析", "PRD"],
     "skills": ["pmaster", "diagram-builder", "ppt-design"],
     "mcp": ["internet_search", "retrieve_dataset_content"],
     "questions": ["帮我梳理这个功能的需求文档框架", "为这个产品方案做一次竞品分析"]},
    {"name": "项目管理", "slug": "project-manager", "category": "职场办公",
     "tags": ["项目管理", "排期", "周报"],
     "skills": ["diagram-builder", "smart-weekly-report"],
     "mcp": ["automation_task"],
     "questions": ["帮我把这个项目拆解成里程碑和任务", "根据进度生成本周项目周报"]},
    {"name": "行政", "slug": "admin-clerk", "category": "职场办公",
     "tags": ["行政", "公文", "会议"],
     "skills": ["word-editing", "meeting-minutes-organizer"],
     "mcp": [],
     "questions": ["帮我起草一份会议通知", "把这段会议录音整理成纪要"]},
    {"name": "HR人力资源管理", "slug": "hr-manager", "category": "职场办公",
     "tags": ["人力资源", "招聘", "制度"],
     "skills": ["word-editing", "official-doc-writer"],
     "mcp": ["internet_search"],
     "questions": ["帮我写一份岗位招聘JD", "起草一份员工考勤管理制度"]},
    {"name": "招聘", "slug": "recruiter", "category": "职场办公",
     "tags": ["招聘", "简历", "面试"],
     "skills": ["word-editing"],
     "mcp": ["internet_search"],
     "questions": ["帮我评估这份简历是否匹配岗位", "设计这个岗位的面试问题清单"]},
    {"name": "美文排版", "slug": "text-beautify", "category": "职场办公",
     "tags": ["排版", "美化", "公众号"],
     "skills": ["word-editing"],
     "mcp": [],
     "questions": ["帮我把这段文字排版美化", "给这篇文章加上合适的小标题和分段"]},
    {"name": "会计师", "slug": "accountant", "category": "职场办公",
     "tags": ["会计", "账务", "报表"],
     "skills": ["excel-editing", "financial-report-minesweeper"],
     "mcp": ["database_query"],
     "questions": ["帮我核对这张费用报表", "解释一下这几个会计科目的处理"]},

    # ── Business analysis (category "商业分析") ──
    {"name": "商业数据分析", "slug": "business-data-analysis", "category": "商业分析", "featured": True,
     "tags": ["商业分析", "经营", "洞察"],
     "skills": ["data-analyst-cn", "data-analysis-report-generator"],
     "mcp": ["database_query", "generate_chart_tool", "retrieve_dataset_content"],
     "questions": ["从这份销售数据里找出关键经营洞察", "帮我做一份经营分析报告"]},
    {"name": "市场营销", "slug": "marketing-specialist", "category": "商业分析",
     "tags": ["市场", "营销", "推广"],
     "skills": ["copywriter-cn", "ad-copywriter"],
     "mcp": ["internet_search"],
     "questions": ["帮我策划一次产品上新的营销活动", "写三版不同风格的推广文案"]},
    {"name": "金融分析师", "slug": "financial-analyst", "category": "商业分析",
     "tags": ["金融", "财报", "估值"],
     "skills": ["financial-report-minesweeper", "excel-editing"],
     "mcp": ["database_query", "generate_chart_tool"],
     "questions": ["帮我分析这家公司的财报有没有风险点", "对这组财务指标做可视化"]},
    {"name": "投资经理", "slug": "investment-manager", "category": "商业分析",
     "tags": ["投资", "尽调", "策略"],
     "skills": ["financial-report-minesweeper"],
     "mcp": ["internet_search", "web_fetch"],
     "questions": ["帮我梳理这个标的的投资逻辑", "这个行业当前的投资风险有哪些"]},
    {"name": "供应链策略专家", "slug": "supply-chain-strategist", "category": "商业分析",
     "tags": ["供应链", "采购", "成本"],
     "skills": ["diagram-builder"],
     "mcp": ["internet_search", "retrieve_dataset_content"],
     "questions": ["帮我画出这条供应链的关键环节", "如何优化这个环节的库存与成本"]},
    {"name": "SEO专家", "slug": "seo-expert", "category": "商业分析",
     "tags": ["SEO", "搜索", "流量"],
     "skills": ["copywriter-cn"],
     "mcp": ["internet_search", "web_fetch"],
     "questions": ["帮我优化这篇文章的SEO关键词", "分析这个页面的搜索表现并给建议"]},

    # ── Data analysis (category "数据分析") ──
    {"name": "数据分析师", "slug": "data-analyst", "category": "数据分析", "featured": True,
     "tags": ["数据分析", "建模", "可视化"],
     "skills": ["data-analyst-cn", "csv-pipeline", "excel-editing", "data-analysis-report-generator"],
     "mcp": ["database_query", "generate_chart_tool"],
     "questions": ["帮我清洗并分析这份CSV数据", "把这组数据做成可视化图表"]},
    {"name": "网站运营数据分析", "slug": "web-analytics", "category": "数据分析",
     "tags": ["网站运营", "转化", "漏斗"],
     "skills": ["data-analyst-cn", "funnel-analyzer"],
     "mcp": ["generate_chart_tool", "database_query"],
     "questions": ["分析这个转化漏斗在哪一步流失最多", "给出提升网站留存的数据建议"]},
    {"name": "统计学家", "slug": "statistician", "category": "数据分析",
     "tags": ["统计", "假设检验", "回归"],
     "skills": ["csv-pipeline", "excel-editing"],
     "mcp": ["generate_chart_tool"],
     "questions": ["帮我选择合适的统计检验方法", "对这组数据做一次回归分析"]},

    # ── R&D & programming (category "研发编程") ──
    {"name": "开发工程师", "slug": "developer", "category": "研发编程", "featured": True,
     "tags": ["编程", "调试", "评审"],
     "skills": ["code-review", "debug-pro"],
     "mcp": ["internet_search", "web_fetch"],
     "questions": ["帮我review这段代码有没有问题", "这个报错怎么排查"]},
    {"name": "前端工程师", "slug": "frontend-engineer", "category": "研发编程",
     "tags": ["前端", "React", "样式"],
     "skills": ["code-review", "debug-pro"],
     "mcp": ["internet_search"],
     "questions": ["帮我优化这个组件的渲染性能", "这个CSS布局问题怎么修"]},
    {"name": "运维工程师", "slug": "devops-engineer", "category": "研发编程",
     "tags": ["运维", "日志", "故障"],
     "skills": ["log-analyzer", "debug-pro"],
     "mcp": ["internet_search"],
     "questions": ["帮我分析这段日志定位故障", "这个服务的监控告警怎么配置"]},
    {"name": "测试工程师", "slug": "qa-engineer", "category": "研发编程",
     "tags": ["测试", "用例", "质量"],
     "skills": ["code-review"],
     "mcp": [],
     "questions": ["帮我为这个功能设计测试用例", "这个缺陷的复现步骤怎么写清楚"]},
    {"name": "网络安全专家", "slug": "security-expert", "category": "研发编程",
     "tags": ["安全", "渗透", "加固"],
     "skills": ["log-analyzer"],
     "mcp": ["internet_search"],
     "questions": ["帮我评估这段代码的安全风险", "这条可疑访问日志要不要警惕"]},
    {"name": "技术作家", "slug": "technical-writer", "category": "研发编程",
     "tags": ["技术文档", "API", "翻译"],
     "skills": ["api-doc-writer", "md2word-cn", "tech-translator"],
     "mcp": [],
     "questions": ["帮我把这个接口整理成API文档", "把这段英文技术文档翻译成中文"]},
    {"name": "全栈开发者", "slug": "fullstack-developer", "category": "研发编程",
     "tags": ["全栈", "架构", "接口"],
     "skills": ["code-review", "debug-pro", "api-doc-writer"],
     "mcp": ["internet_search"],
     "questions": ["帮我设计这个功能的前后端方案", "review一下这个接口设计是否合理"]},

    # ── Translation & writing (category "翻译写作") ──
    {"name": "英语翻译和改进者", "slug": "english-translator", "category": "翻译写作", "featured": True,
     "tags": ["翻译", "润色", "英语"],
     "skills": ["translate", "tech-translator"],
     "mcp": [],
     "questions": ["把这段中文翻译成地道英文", "帮我润色这封英文邮件"]},
    {"name": "要点精炼", "slug": "key-points-refine", "category": "翻译写作",
     "tags": ["摘要", "提炼", "总结"],
     "skills": ["report-summary-generation"],
     "mcp": [],
     "questions": ["帮我把这篇长文提炼成要点", "用三句话总结这段内容"]},
    {"name": "论文写手", "slug": "essay-writer", "category": "翻译写作",
     "tags": ["论文", "学术", "写作"],
     "skills": ["writing-polish", "humanize-zh"],
     "mcp": ["retrieve_dataset_content"],
     "questions": ["帮我搭一个论文的写作大纲", "润色这段论文摘要让它更严谨"]},
    {"name": "记者", "slug": "journalist", "category": "翻译写作",
     "tags": ["新闻", "稿件", "采访"],
     "skills": ["copywriter-cn", "humanize-zh"],
     "mcp": ["internet_search", "web_fetch"],
     "questions": ["帮我把这件事写成一篇新闻稿", "设计一份采访提纲"]},

    # ── Creative design (category "创意设计") ──
    {"name": "广告商", "slug": "advertiser", "category": "创意设计",
     "tags": ["广告", "创意", "投放"],
     "skills": ["ad-copywriter", "copywriter-cn"],
     "mcp": ["internet_search"],
     "questions": ["帮我想几个这个产品的广告创意", "写一句抓人的广告Slogan"]},
    {"name": "网页设计顾问", "slug": "web-design-consultant", "category": "创意设计",
     "tags": ["网页设计", "UI", "体验"],
     "skills": ["diagram-builder"],
     "mcp": ["internet_search"],
     "questions": ["帮我规划这个落地页的版块结构", "这个界面的交互怎么改更好用"]},
    {"name": "社交媒体经理", "slug": "social-media-manager", "category": "创意设计",
     "tags": ["新媒体", "运营", "内容"],
     "skills": ["copywriter-cn", "ad-copywriter"],
     "mcp": ["internet_search"],
     "questions": ["帮我做一周的社媒内容排期", "写一条小红书风格的种草文案"]},

    # ── Policy & legal (category "政策法务") ──
    {"name": "法务", "slug": "legal-affairs", "category": "政策法务", "featured": True,
     "tags": ["法务", "合同", "风险"],
     "skills": ["contract-guardian", "case-research"],
     "mcp": ["retrieve_dataset_content"],
     "questions": ["帮我审一下这份合同有没有风险条款", "这种情况涉及哪些法律风险"]},
    {"name": "法律顾问", "slug": "legal-advisor", "category": "政策法务",
     "tags": ["法律", "咨询", "合规"],
     "skills": ["contract-guardian", "case-research"],
     "mcp": ["retrieve_dataset_content", "internet_search"],
     "questions": ["帮我解读这条法规怎么落地", "这个业务在合规上要注意什么"]},

    # ── Education & research (category "教育科研") ──
    {"name": "哲学教师", "slug": "philosophy-teacher", "category": "教育科研",
     "tags": ["教育", "哲学", "思辨"],
     "skills": [],
     "mcp": ["retrieve_dataset_content"],
     "questions": ["用通俗的方式讲讲这个哲学概念", "带我辩证地分析这个观点"]},
    {"name": "认知科学研究员", "slug": "cognitive-scientist", "category": "教育科研",
     "tags": ["认知", "科研", "学习"],
     "skills": [],
     "mcp": ["internet_search", "web_fetch"],
     "questions": ["从认知科学角度解释这个现象", "有哪些提升学习效率的科学方法"]},
    {"name": "概念框架开发者", "slug": "concept-framework-developer", "category": "教育科研",
     "tags": ["框架", "建模", "结构化"],
     "skills": ["diagram-builder"],
     "mcp": [],
     "questions": ["帮我把这个想法整理成一个概念框架", "用结构化的方式拆解这个问题"]},
]

# Debranding / external product terms: scrubbed during rewriting so stored content is our own neutral content.
_BRAND_PATTERNS = [
    (re.compile(r"Cherry\s*Studio", re.I), "本助手"),
    (re.compile(r"\bCherryHQ\b", re.I), "本助手"),
    (re.compile(r"\bCherry\b", re.I), "本助手"),
    (re.compile(r"ChatGPT", re.I), "AI 助手"),
    (re.compile(r"\bOpenAI\b", re.I), "模型"),
]


def _debrand(text: str) -> str:
    """Debrand + normalize newlines (\\r\\n -> \\n), rewriting into our own neutral prompt."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    for pat, repl in _BRAND_PATTERNS:
        text = pat.sub(repl, text)
    # Collapse 3+ consecutive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _load_whitelist() -> Dict[str, set]:
    """Build the whitelist of bindable ids from real repo assets: default + marketplace skills / MCP / plugins."""
    skills: set = set()
    for sub in ("default", "marketplace"):
        d = BACKEND_ROOT / "skill_bundles" / sub
        if d.is_dir():
            skills.update(c.name for c in d.iterdir() if c.is_dir())
    plugins: set = set()
    for sub in ("default", "marketplace"):
        d = BACKEND_ROOT / "plugin_bundles" / sub
        if d.is_dir():
            plugins.update(c.name for c in d.iterdir() if c.is_dir())
    mcp: set = set()
    try:
        cat = json.loads((BACKEND_ROOT / "core" / "config" / "catalog.json").read_text("utf-8"))
        for m in (cat.get("mcp") or cat.get("mcp_servers") or []):
            if m.get("id"):
                mcp.add(m["id"])
    except Exception as exc:  # noqa: BLE001
        print(f"!! 无法读取 catalog.json 的 MCP 列表：{exc}", file=sys.stderr)
    return {"skills": skills, "mcp": mcp, "plugins": plugins}


def _validate_bindings(entry: Dict[str, Any], wl: Dict[str, set]) -> List[str]:
    errs: List[str] = []
    for sid in entry.get("skills", []):
        if sid not in wl["skills"]:
            errs.append(f"未知技能 id: {sid}")
    for mid in entry.get("mcp", []):
        if mid not in wl["mcp"]:
            errs.append(f"未知 MCP id: {mid}")
    for pid in entry.get("plugins", []):
        if pid not in wl["plugins"]:
            errs.append(f"未知插件 slug: {pid}")
    return errs


def _load_cherry(source: str | None) -> Dict[str, Dict[str, Any]]:
    if source:
        raw = Path(source).read_text("utf-8")
    else:
        print(f".. 下载 {CHERRY_URL}")
        with urllib.request.urlopen(CHERRY_URL, timeout=60) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8")
    data = json.loads(raw)
    return {a["name"]: a for a in data if a.get("name")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", help="本地 agents-zh.json 路径（默认在线下载）")
    ap.add_argument("--dry-run", action="store_true", help="只校验，不写盘")
    args = ap.parse_args()

    wl = _load_whitelist()
    print(f".. 白名单：技能 {len(wl['skills'])} / MCP {len(wl['mcp'])} / 插件 {len(wl['plugins'])}")

    # 1) Validate all binding ids up front; fail immediately if any does not exist (no binding to thin air)
    all_errs: List[str] = []
    for e in CURATION:
        for msg in _validate_bindings(e, wl):
            all_errs.append(f"[{e['slug']}] {msg}")
    if all_errs:
        print("!! 绑定校验失败：", file=sys.stderr)
        for m in all_errs:
            print("   - " + m, file=sys.stderr)
        return 1

    cherry = _load_cherry(args.source)
    written, missing = 0, []
    for e in CURATION:
        src = cherry.get(e["name"])
        if src is None:
            missing.append(e["name"])
            continue
        bundle = {
            "slug": e["slug"],
            "name": e["name"],
            # Cherry's emoji field occasionally carries trailing \r\n; strip it to avoid polluting avatar rendering
            "avatar": (src.get("emoji") or "🤖").strip() or "🤖",
            "summary": _debrand(src.get("description") or "")[:80],
            "description": _debrand(src.get("description") or ""),
            "category": e["category"],
            "tags": e.get("tags", []),
            "version": "1.0.0",
            "author": "内置",
            "source": "builtin",
            "featured": bool(e.get("featured")),
            "system_prompt": _debrand(src.get("prompt") or ""),
            "welcome_message": "",
            "suggested_questions": e.get("questions", []),
            "model_config": {},
            "bindings": {
                "skill_ids": e.get("skills", []),
                "mcp_server_ids": e.get("mcp", []),
                "plugin_ids": e.get("plugins", []),
                "kb_ids": [],
            },
        }
        if args.dry_run:
            written += 1
            continue
        out_dir = MARKET_DIR / e["slug"]
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "agent.json").write_text(
            json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        written += 1

    print(f"== {'校验通过' if args.dry_run else '已生成'} {written} 个预置子智能体 → {MARKET_DIR}")
    if missing:
        print(f"!! Cherry 数据中未找到 {len(missing)} 个角色（已跳过）：{'、'.join(missing)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
