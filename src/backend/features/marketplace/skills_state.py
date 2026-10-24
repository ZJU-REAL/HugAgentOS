"""skills state responsibilities."""
from __future__ import annotations
from extensions.paths import SKILLS_DIR

MARKETPLACE_DIR = SKILLS_DIR / "marketplace"


DEFAULT_SKILLS_DIR = SKILLS_DIR / "default"


MANIFEST_NAME = "marketplace.json"


SKILL_MD_NAME = "SKILL.md"


SECRETS_FILENAME = "secrets.json"


DEFAULT_SKILL_MARKET = {
    "pdf-editing":    {"category": "文档处理", "summary": "生成 / 合并 / 拆分 / 抽页 / 填表单 / 读取 PDF 文档"},
    "capability-guide-brief": {"category": "办公效率", "summary": "速答系统能力清单与使用指引"},
    # OfficeCLI 系列（上游 iOfficeAI/OfficeCLI，Apache 2.0）：靠沙盒预装的
    # ``officecli`` 单文件二进制驱动，是 Word / Excel / PPT 的**唯一**入口——
    # 自研的 word-editing / excel-editing / ppt-design 三个技能已于 2026-08 下线，
    # 能力全部由这一系列承接。三个基座（docx / xlsx / pptx）之上叠场景层，
    # 分工写在各自 SKILL.md 的 description 里，由技能选择器路由。
    "officecli-docx":            {"category": "文档处理", "summary": "OfficeCLI 深度编辑 .docx：TOC / 域 / 脚注 / 公式 / 原生图表 / 批注修订 / schema 校验"},
    "officecli-xlsx":            {"category": "办公效率", "summary": "OfficeCLI 深度编辑 .xlsx：透视表 / 迷你图 / 条件格式 / 数据验证 / 切片器"},
    "officecli-pptx":            {"category": "办公效率", "summary": "OfficeCLI 深度编辑 .pptx：母版版式 / 连接线 / 形状级控制 / 逐页截图质检"},
    "officecli-word-form":       {"category": "文档处理", "summary": "制作可填写 Word 表单：内容控件(SDT) + 复选框 + 邮件合并域 + 文档保护"},
    "officecli-academic-paper":  {"category": "文档处理", "summary": "学术论文排版：APA / Chicago / IEEE / MLA 引用、编号公式、交叉引用、参考文献"},
    "officecli-data-dashboard":  {"category": "数据分析", "summary": "CSV / 表格数据做成 Excel 数据看板：KPI 卡片 + 多图表 + 迷你图 + 条件格式"},
    "officecli-financial-model": {"category": "数据分析", "summary": "Excel 财务模型：三表 / DCF / LBO / 单位经济 / 敏感性分析，全活公式驱动"},
    "officecli-pitch-deck":      {"category": "办公效率", "summary": "融资路演 PPT：种子轮 / A-C 轮 / SAFE / 可转债的投资人 deck"},
    "morph-ppt":                 {"category": "办公效率", "summary": "带 Morph 平滑转场动效的 PPT，自带 40 套成品设计风格包"},
    "morph-ppt-3d":              {"category": "办公效率", "summary": "带 .glb 三维模型与电影级镜头运动的 Morph 动效 PPT"},
}


MARKETPLACE_CATEGORIES = [
    "写作助手", "文档处理", "数据分析", "政策产业",
    "营销创意", "法务合规", "办公效率", "研发效率",
]


SECRETS_SECTION_HEADER = "## 🔑 凭据配置（由安装者提供）"


REQUIRED_SECRETS_SNAPSHOT = "_required_secrets.json"


ADMIN_UPLOAD_OWNER = "__admin_upload__"


ZIP_SKIP_DIR_PARTS = {"__pycache__", ".git", ".svn", ".hg", "__MACOSX"}
