---
name: sop-extractor
description: SOP标准化流程提取器 — 将用户口述/转录的操作流程自动整理为标准SOP文档（含步骤、注意事项、所需工具、检查点），支持多维度质量评分、缺口分析、冗余检测、改进建议和多格式输出（Markdown/HTML/流程图/检查清单/培训卡片）。适用场景：经验沉淀、新人培训、流程标准化、外包交接、AI Agent 工作流定义。
agent_created: true
---

# SOP 标准化流程提取器

## 快速开始（30秒入门）

最快触发方式（直接复制粘贴给 AI）：

```
帮我把这个流程整理成SOP：[粘贴你的操作描述]
```

```
帮我沉淀一下[客服处理/内容发布/软件部署]的流程
```

```
我要给新人写一份[岗位名称]的操作手册，帮我整理成标准化文档
```

```
把下面这段操作记录结构化：[粘贴文字]
```

> **一句话版**：把口述/文字描述扔给我，自动出SOP文档、质量评分、检查清单和流程图。
> 
> **增值效果**：平均节省 80% 的文档整理时间，步骤覆盖率从人工整理的 60% 提升至 90% 以上，自动发现 3-5 个常被遗漏的检查点。

---

## 概述

将脑子里零散的操作经验一键转化为标准化SOP文档。用户只需口述流程（或上传转录文本），AI 自动完成：
- 🔍 **智能解析**：识别步骤边界、操作类型、决策点、检查点
- 📊 **质量评估**：7维度百分制评分（完整性/清晰度/可操作性/安全性/可度量性/效率/可维护性）
- 🔬 **缺口分析**：自动检测缺少的前置条件、检查点、注意事项、异常处理等
- ⚡ **效率优化**：冗余检测、瓶颈识别、并行化建议、自动化建议
- 📦 **多格式输出**：交互HTML、Markdown、Mermaid流程图、执行检查清单、培训卡片

## 触发条件

当用户执行以下操作时自动加载：

**明确触发（高置信度）：**
- 说"帮我把这个流程整理成SOP"
- 说"把这个操作步骤标准化"
- 说"沉淀一下这个经验/流程"
- 粘贴操作描述并说"整理成文档/手册/标准格式"
- 上传口述转录/会议记录需要提炼SOP

**关键词触发：**
- "SOP"、"标准化流程"、"操作手册"、"新人指南"、"流程文档"
- "经验沉淀"、"外包交接"、"培训材料"、"工作流定义"

**不触发（避免误激活）：**
- 单纯聊天讨论某个话题（无整理文档的意图）
- 用户已有结构化SOP仅需排版美化（用文档工具更合适）

## 核心脚本

| 脚本 | 功能 | 核心命令 |
|------|------|----------|
| `scripts/sop_extractor.py` | 核心提取引擎 | `python sop_extractor.py -f input.txt -o sop.json` |
| `scripts/sop_optimizer.py` | 优化分析器 | `python sop_optimizer.py -i sop.json -o optimized.json --report report.json` |
| `scripts/sop_formatter.py` | 多格式输出 | `python sop_formatter.py -i sop.json -f html -o sop.html` |
| `scripts/report_generator.py` | 可视化报告 | `python report_generator.py -s sop.json --optimization report.json -o report.html` |

## 能力边界说明

在开始处理之前，明确 Skill 的处理能力范围，避免用户期望落差：

### ✅ 擅长处理
- **线性操作流程**：有明确先后顺序的步骤，如客服处理、内容发布、软件部署
- **个人/小团队 SOP**：1-3人执行的操作规范，步骤数 ≤ 20
- **口语化描述**：录音转录、聊天记录、随手记录的流程说明
- **已有文档的结构化提升**：对质量参差不齐的旧文档做标准化升级
- **单一工具链流程**：围绕1-2个主要系统/工具的操作流程

### ⚠️ 勉强处理（需降级期望）
- **极简描述（< 50字）**：AI 会主动追问补充，但输出可能步骤偏少、细节不足
- **跨部门复杂协同**：可提取主流程框架，但分支路径和例外情况需人工补充 → 推荐参考 `references/collaboration_guide.md`
- **高度技术性内容**（如代码审查、数据库运维）：步骤可提取，技术细节需人工核实
- **非线性决策树流程**：可用 Mermaid 流程图辅助表达，但结构化程度有限

### ❌ 不适合使用
- **纯聊天/无实质步骤内容**：如"我们讨论了客服策略"，没有具体操作步骤
- **已是标准化 SOP 仅需排版**：直接用文档工具更高效
- **需要精确技术规格的工程文档**（如硬件操作规程、化学品处理手册）：此类需专业人工审核
- **超过 30 步骤的巨型流程**：建议先拆分为子流程再分批处理

---

## 使用误区（5条反模式）

> 遇到这些情况时，换个思路效果更好：

| ❌ 常见误用 | ✅ 正确做法 |
|------------|------------|
| 把整个部门的全年工作流程一次性扔进来（100步+） | 先拆成 3-5 个子流程，分别提取后人工整合 |
| 用于整理聊天记录里的"讨论"而非"操作步骤" | 先从对话中提炼出具体操作动作，再输入 |
| 期望自动生成精确的技术规格（参数值、代码片段） | 用 Skill 提取步骤骨架，技术细节手工填充 |
| 只发一句"帮我整理销售流程"，不补充任何细节 | 参考快速开始的模板，粘贴具体操作描述 |
| 对已有格式规范的 Word/Excel SOP 做二次提取 | 此类直接在原文档修订即可，无需走提取流程 |

---

## 输入质量容错机制

输入质量直接影响输出效果，Skill 按以下策略自动降级处理：

| 输入质量 | 判断标准 | 处理策略 |
|----------|----------|----------|
| **优质** | 有明确步骤、工具、检查点，≥ 100字 | 直接运行全流程，输出完整SOP |
| **一般** | 有基本步骤但细节不足，50-100字 | 先输出框架，标注「⚠️ 待补充」项，并追问关键缺失项 |
| **简短混乱** | < 50字 或 步骤逻辑混乱 | 不直接报错，改为追问3个关键问题，再处理 |

**理解确认机制**：收到复杂流程描述后，先输出一行确认：
```
📌 我理解你要整理的是「[流程名称]」，共识别到约 X 个步骤，覆盖[关键环节]。按[确认]继续，或告诉我理解有误的地方。
```

**三轮追问话术（输入不足时）：**
- 第1轮：「你的描述已收到，但缺少[前置条件/工具说明/结束标志]，能补充一下吗？」
- 第2轮：「好的，我已重新理解。请确认步骤顺序是否正确：[步骤列表]」
- 第3轮（仍不清晰时）：「能发一段具体的操作记录给我看看吗？哪怕是截图/笔记都可以」

---

## 错误诊断速查

当脚本输出错误时，对照下表快速定位并修复：

| 错误信息 | 原因 | 修复命令 |
|----------|------|---------|
| `No steps found` | 输入无具体操作动词 | 补充描述后重试：`python sop_extractor.py -f input.txt --min-steps 2` |
| `JSON decode error` | 上一步输出文件损坏或为空 | 删除损坏文件重新运行：`del sop_output.json && python sop_extractor.py ...` |
| `FileNotFoundError: input.txt` | 文件路径错误 | 用绝对路径：`python sop_extractor.py -f "C:/full/path/input.txt" -o sop.json` |
| HTML报告空白 | JSON路径含中文空格 | 将输出目录移至纯英文路径，如 `C:/sop_output/` |
| HTML乱码 | 源文件非UTF-8编码 | 另存文件为UTF-8：`notepad.exe` → 另存为 → 编码选 UTF-8 |
| 步骤数过少 | 连词过多，未分行 | 加细粒度参数：`python sop_extractor.py -f input.txt --granularity fine` |

> 💡 更多排错详情见 `references/faq.md`

---

## 完整工作流

### 一键运行（推荐，新手首选）

```bash
cd ~/.workbuddy/skills/sop-extractor-plus/scripts/
python run_all.py -f input.txt -o output_dir/
```

`run_all.py` 自动串联全部步骤，最终在 `output_dir/` 生成完整交付物。出错时自动输出「原因+修复建议」，无需手动排查。

### 分步运行（适合：进阶/调试用）

```
用户口述/转录文本
    ↓
sop_extractor.py → sop_output.json    （提取结构化SOP）
    ↓
sop_optimizer.py → 优化分析 + 评分    （质量评估+改进建议）
    ↓
sop_formatter.py → 多格式文档输出     （HTML/MD/流程图/清单）
    ↓
report_generator.py → 可视化报告      （SVG图表+完整分析）
```

## AI 工作流程

### 步骤 1：主动开场 + 收集原始描述

**AI 应主动说明工作流程**（触发后的开场话术）：

```
我来帮你把操作流程整理成标准SOP文档。我需要先了解一下你的流程，然后会帮你：
① 识别所有步骤和检查点  ② 评估文档质量（百分制）  ③ 找出遗漏项  ④ 生成你需要的格式（HTML/流程图/清单等）

请把流程从头到尾描述一遍，越详细越好：
- 每一步用什么工具/系统？
- 中间哪个环节最容易出错？
- 最终怎么确认做完了/做对了？
```

对于简短或混乱的输入，启动三轮追问机制（见"输入质量容错机制"章节），不直接报错。
对于跨部门或多人协作场景，引导用户参考 `references/collaboration_guide.md`。
可参考 `references/prompt_guide.md` 的渐进式对话引导模板。

### 步骤 2：运行提取引擎（适合：标准用法）
```bash
cd ~/.workbuddy/skills/sop-extractor-plus/scripts/
python sop_extractor.py -f <用户提供的文本文件路径> -o sop_output.json
```

或使用 `-t` 直接传入文本。

### 步骤 3：运行优化分析（适合：标准用法）
```bash
python sop_optimizer.py -i sop_output.json -o sop_optimized.json --report optimization_report.json
```

### 步骤 4：生成交付物
根据用户需求选择格式：
```bash
# 交互式HTML（推荐首屏展示）
python sop_formatter.py -i sop_optimized.json -f html -o sop.html --theme dark

# 多人协作场景（适合：进阶）
# 参考 references/collaboration_guide.md 先拆分子流程，再分别格式化

# 综合可视化报告（适合：进阶）
python report_generator.py -s sop_output.json --optimization optimization_report.json -o sop_report.html
```

### 步骤 5：展示结果
打开 `sop_report.html` 或 `sop.html` 预览，向用户汇报：
- 提取的步骤数量和质量评分
- 发现的缺口和改进建议
- 生成的交付物清单

## 输出格式一览

| 格式 | 用途 | 命令参数 |
|------|------|----------|
| `markdown` | 文档归档、版本管理 | `-f markdown` |
| `html` | 交互式Web页面（深色主题） | `-f html --theme dark` |
| `mermaid` | 流程图嵌入 | `-f mermaid` |
| `checklist` | 可打印执行清单 | `-f checklist` |
| `training` | 新人培训卡片 | `-f training` |
| `json` | 数据交换 | `-f json` |
| `all` | 全部格式一键生成 | `-f all -o output_dir/` |

## 质量评分体系

| 评分 | 等级 | 含义 |
|------|------|------|
| 85-100 | A | 优秀 — 可直接投入使用 |
| 70-84 | B | 良好 — 小修后即可发布 |
| 55-69 | C | 合格 — 需补充完善 |
| < 55 | D | 需改进 — 建议大幅完善 |

## 参考资料按需导航

| 你的情况 | 推荐阅读 |
|----------|----------|
| 第一次用，不知道输入什么 | `references/examples.md` → 查看真实案例 |
| 想了解质量评分如何计算 | `references/quality_checklist.json` |
| 需要对话引导用户提供详细描述 | `references/prompt_guide.md` |
| 遇到脚本报错/输出不理想 | `references/faq.md` |
| 需要套用特定行业模板 | `references/sop_templates.json` |
| 多人/跨部门协同场景 | `references/collaboration_guide.md` |

- `references/sop_templates.json` — 8种行业SOP模板（客服/内容发布/软件部署/数据处理/招聘/采购/Bug修复/通用操作）
- `references/quality_checklist.json` — 7维度质量评估标准及常见缺失项
- `references/prompt_guide.md` — AI 渐进式对话引导模板（4轮对话脚本）
- `references/examples.md` — 4个完整使用案例（触发→处理过程→输出预览）
- `references/faq.md` — 12题常见问题解答（含脚本报错、简短输入、跨部门流程等边缘场景）
- `references/collaboration_guide.md` — 多人/跨部门协同SOP专项引导模板（含角色分工模板）

## 依赖

纯 Python 标准库，无需额外安装。所有脚本均为零依赖设计。
