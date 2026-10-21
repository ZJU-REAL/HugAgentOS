---
name: data-mask
description: >
  敏感数据脱敏工具。当用户需要对文本中的数字进行脱敏处理时使用此技能——包括将文件或对话中的真实数字替换为模糊值、
  区间、占位符或哈希映射值。支持 4 种脱敏策略（hash/blur/placeholder/range），覆盖 12+ 种文件格式。
  
  触发场景：用户说"帮我把这段文字的数字脱敏"、"把报告里的敏感数据模糊化"、
  "脱敏后发给大模型"、"把这个文件里的数字都替换掉"、"对这篇文档做数据脱敏"、
  "这份数据要发给外部但不想暴露真实数字"、"表格数据脱敏"、"Word文档脱敏"、"PPT脱敏"、
  "图片里的数字帮我模糊化"、"PDF脱敏"、"批量脱敏多个文件"。
  
  子模块路由：
  - 提到「Excel」「CSV」「表格」「列」→ 触发 dataframe_mask.py（列级精准脱敏）
  - 提到「Word」「docx」「PPT」「pptx」「PDF」「图片」「HTML」「Markdown」→ 触发 file_mask.py（文档级脱敏）
  - 提到「文字」「文本」「段落」「粘贴」→ 触发 mask.py（纯文本脱敏）
  - 提到「还原」「恢复」「unmask」→ 触发映射表还原模式
  - 提到「批量」「目录下所有」→ 触发批量处理模式
  未指定策略时默认使用哈希映射(hash)，脱敏完成后提示其他可用策略及效果对比。

  采用混合策略处理不同文件格式：
  内置解析 + 同格式输出：Word(.docx)、PowerPoint(.pptx)、Excel(.xlsx/.csv)、Markdown(.md)、纯文本(.txt)
  内置解析 → .txt 输出：HTML(.html)、PDF(.pdf)、图片(.png/.jpg/.gif/.bmp/.webp/.tiff)
  委托专项 skill 提取文本后脱敏：扫描版 PDF、复杂 HTML
  未知格式回退：自动尝试文本读取 → .txt 输出
category: data-tools
tags:
  - 数据脱敏
  - 隐私保护
  - 文档处理
  - Excel
  - Word
language: zh-CN
---

# 数据脱敏工具 (Data Mask)

对文档和文本中的敏感数字数据自动识别并脱敏，解决日常文件或发送给大模型时需要手动修改真实数字的重复劳动。

---

## 🎯 新手入门（直接复制就能用）

**不需要读完文档，直接把需求发过来就行。几个典型开场白：**

```
✅ "帮我把这段文字的敏感数字脱敏"
✅ "这份 Excel 表格帮我脱敏，只脱敏金额列"
✅ "这个 Word 报告要发给外包团队，帮我把数字都模糊化"
✅ "这份销售数据发给 AI 做分析，帮我脱敏但要能还原"
✅ "这个目录下所有 .xlsx 文件都脱敏一下"
```

**脱敏后默认输出：** `原文件名_mask.原后缀`（如 `report.docx` → `report_mask.docx`）

---

## 能力边界说明

### ✅ 擅长处理
1. 对 Word/PPT/Excel/CSV/Markdown/纯文本中的数字自动识别并脱敏
2. 保留 Word/PPT 原始排版、字体、表格、页眉页脚等全部格式
3. 通过映射表完整还原 hash 和 placeholder 策略脱敏后的数据
4. 批量处理同目录下多个文件
5. 自动识别并跳过非敏感数字（ID、序号、年份等）
6. 对图片进行 OCR 识别后脱敏（需安装 Tesseract）

### ⚠️ 需要素材/前置条件
1. **图片 OCR 脱敏**：需要安装 Tesseract OCR 引擎（文档提供各平台安装指引）
2. **Excel 列级精准控制**：需要提供具体的列名（或让工具自动检测数值列）
3. **还原数据**：需要脱敏时保存的映射表 JSON 文件（脱敏时加 `-m` 参数）
4. **自定义单位**：如需识别非标准单位后缀，需要在 `config.yaml` 中配置

### ❌ 超出范围（附替代方案）
1. **.doc / .xls 旧格式**：不支持旧版 Office 格式 → 先手动转为 .docx / .xlsx
2. **扫描版 PDF（无文字层）**：自动委托 PDF skill 做 OCR → 输出为 .txt
3. **JS 渲染的复杂 HTML（SPA 页面）**：自动委托浏览器 skill 提取文本 → 输出为 .txt
4. **数据加密**：本工具是脱敏工具非加密工具 → 需要加密请使用专业加密工具
5. **非数字敏感信息**（人名、地名等）：工具只脱敏数字 → 对非数字敏感信息需人工处理

---

## 📛 输出文件命名规范

脱敏后的文件统一遵循以下命名规则：

```
原文件名_mask.原后缀
```

| 输入文件 | 脱敏后输出 |
|---------|----------|
| `运营月报.docx` | `运营月报_mask.docx` |
| `Q2数据.xlsx` | `Q2数据_mask.xlsx` |
| `汇报PPT.pptx` | `汇报PPT_mask.pptx` |
| `notes.md` | `notes_mask.md` |
| `page.html` | `page_mask.txt`（HTML 固定输出 .txt） |
| `screenshot.png` | `screenshot_mask.txt`（图片 OCR 固定输出 .txt） |
| `report.pdf` | `report_mask.pdf`（或 `report_mask.txt`，见 PDF 说明） |

> **如果需要自定义输出路径**，所有工具也支持通过 `-o` / `--output` 参数手动指定。

---

## 🚀 快速开始（30 秒上手）

**我最常用的场景是？** 选一个直接看：

| 你手里有什么 | 去哪里看 |
|------------|---------|
| 一段文字/粘贴的文本 | → [纯文本脱敏](#方式1纯文本脱敏最常用) |
| Excel 表格 (.xlsx/.csv) | → [表格脱敏](#方式2-excelcsv-表格脱敏) |
| Word 文档 (.docx) | → [Word 脱敏](#方式3-word-文档脱敏) |
| PPT 演示文稿 (.pptx) | → [PPT 脱敏](#方式4-powerpoint-脱敏) |
| PDF 文件 | → [PDF 脱敏](#方式5-pdf-脱敏) |
| 不了解策略，想先选一个 | → [策略选择指南](#策略选择指南) |
| 遇到报错/问题 | → [常见错误与排障](#常见错误与排障) |
| 需要批量处理多个文件 | → [批量处理](#方式12批量处理多个文件) |

---

## 模块结构

| 文件 | 用途 | 适用场景 |
|------|------|---------|
| `scripts/mask.py` | 纯文本脱敏核心 | 文本内容、对话粘贴 |
| `scripts/dataframe_mask.py` | 结构化数据脱敏 | Excel/CSV 表格，支持列级控制 |
| `scripts/file_mask.py` | 文档级脱敏（统一入口） | Word/PPT/PDF/图片/HTML/Markdown/TXT |
| `config.yaml` | 可扩展配置 | 自定义单位、列过滤规则、回退参数 |

---

## 策略选择指南

**不知道怎么选策略？按这个决策流程走：**

```
你脱敏完的数据要用来干嘛？
├─ 发给 AI 分析，结论要对应回真实数据
│  → 选 hash（默认）  ← 最常用，推荐首选
├─ 发给完全不信任的外部，一点都不想暴露量级
│  → 选 placeholder
├─ 内部团队分享，数据还要能做趋势分析/画图表
│  → 选 blur
├─ 对外发布，知道个大概范围就够了
│  → 选 range
└─ 不确定
   → 用默认 hash，最稳妥
```

### 四种策略效果对比

以数字 `158000万元` 和 `23.5%` 为例：

| 策略 | `158000万元` 脱敏后 | `23.5%` 脱敏后 | 能还原 | 推荐场景 |
|------|-------------------|--------------|:--:|------|
| **hash**（默认） | `443350万元` | `77%` | ✅ | 日常分析、需还原 |
| **placeholder** | `[A]万元` | `[B]` | ✅ | 极致安全 |
| **blur** | `162350万元` | `24.1%` | ❌ | 内部分享、做分析 |
| **range** | `[110600~205400]万元` | `[16.5%~30.6%]` | ❌ | 对外发布 |

### 策略特性速查

| | hash | placeholder | blur | range |
|------|:--:|:--:|:--:|:--:|
| **数值看起来像真的** | ✅ | ❌ | ✅ | ⚠️ |
| **同一数字脱敏后一致** | ✅ | ✅ | ❌ | ❌ |
| **支持还原** | ✅ | ✅ | ❌ | ❌ |
| **保留数量级** | ✅ | ❌ | ✅ | ✅ |
| **安全等级** | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐ | ⭐⭐⭐⭐ |

---

## 架构概览

本 skill 采用**内置解析为主、专项 skill 为补充**的混合策略：

```
┌─ 输入文件 ─────────────────────────────────────────────────────┐
│                                                                │
│  ├─ 原生支持（同格式输出）───────────────────────────────────   │
│  │   .docx → 保留字体/表格/页眉页脚 → .docx                    │
│  │   .pptx → 保留幻灯片/文本框/表格  → .pptx                    │
│  │   .xlsx → 列级精准脱敏           → .xlsx                    │
│  │   .csv  → 列级精准脱敏           → .csv                     │
│  │   .md   → 保留 Markdown 语法     → .md                      │
│  │   .txt  → 纯文本脱敏             → .txt                     │
│  │                                                             │
│  ├─ 文本提取（.txt 输出）───────────────────────────────────    │
│  │   .html → 剥离标签/保留段落结构  → .txt                     │
│  │   .pdf  → PyPDF2 提取文字       → .pdf 或 .txt             │
│  │   .png/.jpg/... → OCR识别 → 脱敏 → .txt                    │
│  │                                                             │
│  └─ 回退/委托 ─────────────────────────────────────────────    │
│      .json/.xml/.yaml → 文本读取 → mask.py 脱敏 → .txt         │
│      扫描版 PDF → PDF skill OCR → mask.py 脱敏 → .txt          │
│      复杂 HTML  → 浏览器 skill 提取 → mask.py 脱敏 → .txt      │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

---

## 使用方式

### 方式1：纯文本脱敏（最常用）

通过管道或文件输入：

```bash
# 管道输入
echo "今年营收12345万元，同比增长15.3%" | python scripts/mask.py

# 文件输入 → 自动生成输出文件（原文件名_mask.原后缀）
python scripts/mask.py --input report.txt --strategy hash

# 或手动指定输出文件
python scripts/mask.py --input report.txt --output masked.txt --strategy hash
```

**输入 → 输出示例（hash 策略）：**

```
输入: 公司2025年实现营业收入158000万元，同比增长23.5%。
输出: 公司8729年实现营业收入443350万元，同比增长77%。
```

---

### 方式2：Excel/CSV 表格脱敏

智能识别数值列，自动跳过 ID、序号、年份等非敏感列。

```bash
# 自动检测数值列并脱敏（自动生成 data_mask.xlsx）
python scripts/dataframe_mask.py -i data.xlsx

# 手动指定输出
python scripts/dataframe_mask.py -i data.xlsx -o masked.xlsx

# 手动指定列
python scripts/dataframe_mask.py -i data.xlsx -c "销售额,利润"

# 保存映射表，支持还原
python scripts/dataframe_mask.py -i data.xlsx -m mapping.json
python scripts/dataframe_mask.py --unmask -i masked.xlsx -o restored.xlsx -m mapping.json
```

**输入 → 输出示例（hash 策略）：**

| 城市 | 日均订单（脱敏前） | 日均订单（脱敏后） | 完单率（脱敏前） | 完单率（脱敏后） |
|------|------------------|------------------|----------------|----------------|
| 北京 | 853000 | 720000 | 94.2% | 47% |
| 上海 | 621000 | 700000 | 93.8% | 23% |
| 广州 | 487000 | 195000 | 91.5% | 10% |

---

### 方式3：Word 文档脱敏

全文脱敏，保留字体、字号、表格、页眉页脚全部格式。

```bash
# 全文脱敏（自动生成 report_mask.docx）
python scripts/file_mask.py -i report.docx

# 手动指定输出路径
python scripts/file_mask.py -i report.docx -o masked_report.docx

# 指定策略
python scripts/file_mask.py -i report.docx -s placeholder
```

> **格式保留**：脱敏后的 Word 和原始文档排版完全一致，只是数字变了。表格内数字、页眉页脚中的日期/数字也一并处理。

---

### 方式4：PowerPoint 脱敏

遍历所有幻灯片、文本框、表格、组合形状。

```bash
# 全文脱敏（自动生成 slides_mask.pptx）
python scripts/file_mask.py -i slides.pptx

# 手动指定输出
python scripts/file_mask.py -i slides.pptx -o masked_slides.pptx

# 模糊化策略
python scripts/file_mask.py -i slides.pptx -s blur
```

> **处理范围**：每页幻灯片中的文本框、表格单元格、嵌套组合图形中的文字——全部逐页遍历处理。

---

### 方式5：PDF 脱敏

```bash
# 生成新 PDF（自动生成 document_mask.pdf）
python scripts/file_mask.py -i document.pdf

# 输出为纯文本（自动生成 document_mask.txt）
python scripts/file_mask.py -i document.pdf --as-text

# 保存映射表
python scripts/file_mask.py -i document.pdf -m mapping.json
```

> **注意**：PDF 脱敏依赖 PyPDF2 和 fpdf2 库。扫描版 PDF（无文字层）会自动委托 PDF skill 做 OCR。

---

### 方式6：HTML 脱敏

```bash
# 剥离标签 → 保留段落结构 → 脱敏 → .txt 输出
python scripts/file_mask.py -i page.html -o masked.txt
python scripts/file_mask.py -i page.html -o masked.txt -s placeholder
```

> **限制**：HTML 脱敏固定输出为 .txt，无法写回原 HTML 结构。

---

### 方式7：Markdown 脱敏

```bash
# 数字替换不影响 Markdown 语法，输出仍为 .md
python scripts/file_mask.py -i notes.md -o masked_notes.md
python scripts/file_mask.py -i notes.md -o masked.md -s blur
```

---

### 方式8：图片脱敏（OCR）

```bash
# ⚠️ 前置条件：需安装 Tesseract OCR 引擎（见下方说明）
python scripts/file_mask.py -i screenshot.png -o masked.txt --ocr-lang chi_sim+eng

# 纯英文图片
python scripts/file_mask.py -i chart.jpg -o masked.txt --ocr-lang eng
```

**Tesseract OCR 安装（首次使用需要）：**

| 系统 | 安装方式 |
|------|---------|
| Windows | 下载安装：<https://github.com/UB-Mannheim/tesseract/wiki> |
| macOS | `brew install tesseract tesseract-lang` |
| Linux | `apt-get install tesseract-ocr tesseract-ocr-chi-sim` |
| Python | `pip install pytesseract Pillow` |

若未安装 Tesseract 就直接使用，会收到明确的安装指引提示。

---

### 方式9：未知格式回退

```bash
# JSON / XML / YAML 等文本格式自动尝试多编码读取并脱敏
python scripts/file_mask.py -i data.json -o masked.txt
python scripts/file_mask.py -i config.xml -o masked.txt
```

---

### 方式10：统一入口（编程调用）

```python
from file_mask import mask_document

# 一行代码，自动识别格式（output_path 可选，传 None 自动按规范命名）
mask_document('report.docx', None, strategy='hash')      # → report_mask.docx
mask_document('slides.pptx', None, strategy='blur')       # → slides_mask.pptx
mask_document('data.pdf', None, strategy='range')         # → data_mask.pdf
mask_document('data.xlsx', None)                           # → data_mask.xlsx
mask_document('page.html', None, strategy='hash')         # → page_mask.txt
mask_document('notes.md', None, strategy='placeholder')   # → notes_mask.md
mask_document('screenshot.png', None, ocr_lang='chi_sim+eng')  # → screenshot_mask.txt
```

---

### 方式11：还原脱敏数据

```bash
# 文本还原
python scripts/mask.py --unmask -i masked.txt -o restored.txt -m mapping.json

# Excel 还原
python scripts/dataframe_mask.py --unmask -i masked.xlsx -o restored.xlsx -m mapping.json
```

> **注意**：只有 hash 和 placeholder 策略支持还原。blur 和 range 策略不可逆。

---

### 方式12：批量处理多个文件

```bash
# 批量脱敏同一目录下的所有 Word 文档（自动生成 *_mask.docx）
for f in *.docx; do
    python scripts/file_mask.py -i "$f" -s hash
done

# 批量脱敏所有 Excel 文件并保存映射
for f in *.xlsx; do
    python scripts/dataframe_mask.py -i "$f" -m "${f%.xlsx}_map.json"
done
```

---

## 🔒 安全与隐私说明

### 数据处理原则

1. **本地处理**：所有脱敏操作在本地机器完成，不将数据上传到任何云端服务
2. **不存储原始数据**：脱敏脚本读取文件后直接处理输出，不保留原始数据副本
3. **映射表安全**：映射表（JSON 文件）包含原始数字到脱敏数字的对应关系，相当于「还原密钥」，请单独保管，不要和脱敏文件一同发送
4. **脱敏后需人工复核**：工具只替换数字，不做语义级安全检查。脱敏后的数据中可能仍包含非数字敏感信息（人名、地名、特定表述等），发往外部前请通读确认

### 禁止行为

- ❌ 不要将映射表上传到公开的 Git 仓库或云盘
- ❌ 不要将映射表和脱敏文件一起发送给第三方
- ❌ 不要在未人工复核的情况下将脱敏数据发送给完全不信任的外部方
- ❌ 不要用本工具处理需要加密才能满足合规要求的数据（本工具是脱敏，不是加密）

### 不同策略的安全等级

| 策略 | 安全等级 | 适用场景 |
|------|:--:|------|
| placeholder | ⭐⭐⭐⭐⭐ | 发给完全不信任的外部，极致安全 |
| range | ⭐⭐⭐⭐ | 对外发布，知道大概范围 |
| hash | ⭐⭐⭐ | 发给 AI 分析，日常最常用 |
| blur | ⭐⭐ | 内部分享，趋势分析 |

---

## 输出准确性约束

系统处理遵循以下规则确保输出可靠：

- **不确定的逻辑先行标注**：当脱敏策略在边界情况（如超大数字、非标准单位）下行为不确定时，在输出中标注假设条件
- **不胡编数据**：脱敏后的数字严格基于原始数据通过确定性算法生成（hash）或基于量级区间采样（blur/range），不会凭空生成
- **映射表可验证**：hash 和 placeholder 策略生成映射表 JSON，可事后验证每次替换的准确性
- **已知限制透明**：文档中已列出 7 项已知限制（PDF 排版、HTML 输出格式等），不隐瞞工具的局限性

---

## 异常处理规则

当用户输入信息不足时，遵循「先给默认行为 + 列具体缺失项」原则，**禁止**笼统回复「请提供更多信息」：

```
示例：用户只说「帮我脱敏这个文件」

✅ 正确回复：
"我先用默认策略（hash）对文件做全文脱敏。输出文件：xxx_mask.docx。
如果这不是你想要的，需要补充：
1. 想用哪个策略？hash（可还原）/ blur（模糊化）/ placeholder（完全隐藏）/ range（区间）
2. 需要保存映射表吗？（加 -m 参数）
3. 只要脱敏特定列吗？（Excel场景，用 -c 指定列名）"

❌ 错误回复：
"请提供更多信息"
```

---

## 受众说明

| 用户类型 | 如何使用 |
|---------|---------|
| **日常办公用户** | 直接复制「新手入门」中的开场白触发，工具自动选择默认策略（hash）处理 |
| **数据分析师** | 使用 `dataframe_mask.py` 做列级精准脱敏，保存映射表以便事后还原 |
| **团队协作** | 批量处理多文件时使用统一映射表（同一 `-m` 参数），确保跨文件数字一致性 |
| **CI/CD 集成** | 通过 Python API 调用（`from file_mask import mask_document`），传入策略和输出路径 |
| **安全管理** | 使用 placeholder 策略做高安全等级脱敏，配合人工复核后发往外部 |

---

## 定制化使用指南

### 配置文件定制 (config.yaml)

```yaml
# 追加自定义单位后缀（如"人次"、"笔订单"）
custom_units:
  - "人次"
  - "笔订单"

# Excel 列规则：强制脱敏/跳过脱敏
column_rules:
  force_mask_patterns:
    - ".*金额"   # 所有含"金额"的列强制脱敏
  skip_patterns:
    - "^ID$"     # ID 列跳过脱敏

# 默认参数：无需每次命令行指定
defaults:
  strategy: hash     # 默认策略，可改为 blur/placeholder/range
  seed: 42           # 随机种子，确保可复现
```

### 参数传递定制

| 需求 | 参数 | 示例 |
|------|------|------|
| 只脱敏不还原 | 不加 `-m` | `python scripts/file_mask.py -i report.docx` |
| 脱敏并保存映射 | `-m mapping.json` | `python scripts/file_mask.py -i report.docx -m map.json` |
| 指定输出路径 | `-o output.docx` | `python scripts/file_mask.py -i in.docx -o out.docx` |
| 跳电话号码 | `--skip-phone` | `python scripts/file_mask.py -i report.docx --skip-phone` |
| 纯英文 OCR | `--ocr-lang eng` | `python scripts/file_mask.py -i chart.png --ocr-lang eng` |

---

## 参考文档

深度使用请查阅以下 reference 文件：

- `references/anti-patterns.md` — 最常见误用场景 + 正确做法对比 + 禁忌清单
- `references/faq-deep.md` — 深度 FAQ（边缘场景/安全合规/CICD 集成，9 题）
- `references/examples.md` — 6 个完整使用示例（文本/Excel/Word/PPT/图片/批量）

---

## 常见问题 FAQ

**Q1：脱敏后的数字为什么看起来怪怪的？**
这是正常现象。hash 策略用确定性算法替换数字，脱敏后的数字和原始值完全不同——这正是设计目标。如果不希望数据看起来太假，请用 blur 策略；如果要彻底隐藏，请用 placeholder。

**Q2：我可以只脱敏 Excel 中的部分列吗？**
可以。用 `-c` 参数指定列名（逗号分隔）：`python scripts/dataframe_mask.py -i data.xlsx -c "销售额,利润"`。不指定时工具自动检测数值列。

**Q3：脱敏后还能还原吗？**
只有 hash 和 placeholder 策略支持还原，需要脱敏时保存的映射表。脱敏时加 `-m mapping.json`，还原时用 `--unmask -m mapping.json`。blur 和 range 策略不可逆。

**Q4：Word/PPT 脱敏后格式会乱吗？**
不会。工具保留原文档的全部格式（字体、字号、颜色、表格结构、页眉页脚等），只替换数字内容。

**Q5：图片里的数字能脱敏吗？**
可以。需要先安装 Tesseract OCR 引擎（各平台安装指引见「方式8」），然后通过 OCR 识别文字并脱敏，输出为 .txt 文件。

**Q6：脱敏后的数据发给 AI 大模型，模型平台会存储吗？**
不会存储你的原始数据，因为脱敏后已不包含真实数字。但建议：敏感度极高的数据用 placeholder 策略，完全不保留数值信息。

---

## 参数说明

### file_mask.py（文档脱敏统一入口）

| 参数 | 说明 |
|------|------|
| `-i, --input` | 输入文件路径（.docx/.pptx/.pdf/.png/.jpg/.html/.md/.txt/.json...，**必填**） |
| `-o, --output` | 输出文件路径（**可选**，默认：原文件名_mask.原后缀） |
| `-s, --strategy` | 脱敏策略：hash/placeholder/blur/range（默认 hash） |
| `-m, --map-file` | 映射表输出路径（JSON，仅 hash/placeholder 可还原） |
| `--seed` | 随机种子（默认 42） |
| `--skip-phone` | 跳过电话号码 |
| `--as-text` | PDF 脱敏后输出为 .txt 而非 PDF |
| `--ocr-lang` | 图片 OCR 语言（默认 chi_sim+eng，纯英文用 eng） |

### dataframe_mask.py（表格脱敏）

| 参数 | 说明 |
|------|------|
| `-i, --input` | 输入文件路径（.xlsx/.csv，**必填**） |
| `-o, --output` | 输出文件路径（**可选**，默认：原文件名_mask.原后缀） |
| `-c, --columns` | 手动指定脱敏列（逗号分隔），默认自动检测数值列 |
| `-s, --strategy` | 脱敏策略（默认 hash） |
| `-m, --map-file` | 映射表输出路径（JSON） |
| `--seed` | 随机种子（默认 42） |
| `--skip-phone` | 跳过电话号码 |
| `--unmask` | 还原模式，需配合 `-m` |
| `--sheet-name` | Excel 工作表名或索引（默认 0） |
| `--encoding` | CSV 文件编码（默认 utf-8） |

### mask.py（纯文本脱敏）

| 参数 | 说明 |
|------|------|
| `-s, --strategy` | 脱敏策略（默认 hash） |
| `-i, --input` | 输入文件路径 |
| `-o, --output` | 输出文件路径（**可选**，默认：原文件名_mask.原后缀） |
| `-m, --map-file` | 映射表 JSON 路径 |
| `--unmask` | 还原模式，需配合 `-m` |
| `--seed` | 随机种子（默认 42） |
| `--skip-phone` | 跳过电话号码 |

---

## 支持的文件格式总览

| 格式 | 扩展名 | 处理方式 | 输出格式 | 保留结构 | 还原 |
|------|--------|---------|---------|---------|:--:|
| 纯文本 | .txt | mask.py | .txt | - | hash/placeholder |
| Markdown | .md | 按纯文本 | .md | ✅ | hash/placeholder |
| Excel | .xlsx/.xls | dataframe_mask | .xlsx | ✅ | hash/placeholder |
| CSV | .csv | dataframe_mask | .csv | ✅ | hash/placeholder |
| Word | .docx | file_mask | .docx | ✅ 格式/表格/页眉页脚 | hash/placeholder |
| PPT | .pptx | file_mask | .pptx | ✅ 幻灯片/文本框/表格 | hash/placeholder |
| PDF | .pdf | PyPDF2+fpdf2 | .pdf/.txt | ⚠️ 文字内容保留 | hash/placeholder |
| HTML | .html/.htm | stdlib 解析 | .txt | ⚠️ 文本+段落 | hash/placeholder |
| 图片 | .png/.jpg/.gif/.bmp/.webp/.tiff | Tesseract OCR | .txt | - | hash/placeholder |
| JSON等 | .json/.xml/.yaml | 文本回退 | .txt | - | hash/placeholder |
| 扫描PDF | .pdf | 委托 PDF skill | .txt | - | hash/placeholder |
| 复杂HTML | .html | 委托浏览器 skill | .txt | - | hash/placeholder |

---

## 性能与能力边界

### 处理速度参考

| 文件类型 | 典型大小 | 预计耗时 |
|---------|---------|---------|
| 纯文本 (.txt) | 1 MB（约 10 万字） | < 1 秒 |
| Markdown (.md) | 500 KB | < 1 秒 |
| Excel (.xlsx) | 10 MB（约 5 万行） | 3~8 秒 |
| Word (.docx) | 50 页 | 2~10 秒 |
| PPT (.pptx) | 100 页 | 3~15 秒 |
| PDF（文字型） | 100 页 | 5~20 秒 |
| 图片 OCR | 1920×1080 截图 | 2~10 秒 |

> **大文件建议**：超过 100 MB 的 Excel 或超过 200 页的 Word/PPT，建议先拆分后再脱敏。

### 已知限制

1. **PDF 排版**：脱敏后生成的 PDF 仅保留文字内容和分页，不保留原始排版、图片、表格样式
2. **HTML 输出**：HTML 脱敏固定输出 .txt，无法写回原 HTML 结构
3. **图片 OCR**：依赖外部 Tesseract 引擎安装；手写体、模糊图片识别率有限
4. **编码**：纯文本文件默认用 UTF-8 读写；遇到 GBK/GB2312 编码会自动回退尝试
5. **.doc / .xls 旧格式**：不支持旧版 Office 格式，需先转为 .docx / .xlsx
6. **超大数字**：超过 10 位的整数可能因浮点精度问题产生微小偏差
7. **并发安全**：脚本非线程安全，不建议多进程同时写同一个输出文件

---

## 可配置选项 (config.yaml)

```yaml
custom_units:        # 追加自定义单位后缀
column_rules:        # Excel 列名匹配规则
  force_mask_patterns:   # 强制脱敏的列（正则）
  skip_patterns:         # 跳过脱敏的列（正则）
skip_rules:          # 排除特定格式
defaults:            # 默认参数
pdf:                 # PDF 输出设置
ocr:                 # OCR 语言和路径配置
fallback:            # 未知格式回退参数
```

---

## 常见错误与排障

### ❌ 错误："文件不存在" / "No such file"

```
错误：文件不存在: /path/to/file.xlsx
```

**原因与解决：**
- 检查文件路径是否拼写正确，注意相对路径和绝对路径的区别
- Windows 下路径包含中文时，确认终端编码设置正确
- 如果是拖拽文件到终端，确认路径中没有多余的空格或引号

---

### ❌ 错误：缺少依赖库

```
错误：需要安装 python-docx: pip install python-docx
```

**原因与解决：**
- 不同文件格式需要不同的 Python 包，按提示安装即可
- 常见依赖：`python-docx`（Word）、`python-pptx`（PPT）、`openpyxl`（Excel）、`PyPDF2` + `fpdf2`（PDF）、`pytesseract` + `Pillow`（图片 OCR）
- **一键安装所有依赖**：`pip install python-docx python-pptx openpyxl pandas PyPDF2 fpdf2`

---

### ❌ 错误：Excel 列名不存在

```
警告：列 "销售额" 不存在，已跳过
```

**原因与解决：**
- 用 `-c` 指定列名时，列名必须和 Excel 表头完全一致（包括空格、标点）
- 如果不确定列名，不带 `-c` 参数运行，工具会自动检测数值列
- 可以先用 `-c` 不指定列跑一次，看输出日志中显示的列名

---

### ❌ 错误：还原时映射表不存在

```
错误：还原模式需要提供映射表文件 (--map-file)
```

**原因与解决：**
- 还原（`--unmask`）必须配合之前脱敏时保存的映射表 JSON 文件
- 脱敏时记得加 `-m mapping.json` 保存映射表
- 映射表文件和脱敏后的文件要配对使用，不能混用

---

### ❌ 错误：CSV 中文乱码

**原因与解决：**
- CSV 默认用 UTF-8 编码读取。如果你的 CSV 是 GBK 编码，加 `--encoding gbk`
- 如果有 BOM 头的 UTF-8，尝试 `--encoding utf-8-sig`

---

### ❌ 错误：PDF 生成失败

```
PDF 生成失败，保存为文本文件
```

**原因与解决：**
- 通常是因为系统缺少中文字体，工具会自动回退到 .txt 输出
- 如需 PDF 输出，确保系统安装了中文字体（如微软雅黑、宋体）
- 也可以直接加 `--as-text` 参数指定输出为文本

---

### ❌ 错误：OCR 未识别到文字

```
警告: OCR 未识别到文字内容，输出为空文件
```

**原因与解决：**
- 图片可能不含文字，或文字太小/太模糊
- 纯中文图片试试 `--ocr-lang chi_sim`（不加 eng）
- 确认 Tesseract 已正确安装：命令行输入 `tesseract --version` 检查
- Windows 用户确认安装时勾选了中文语言包

---

### ❌ 错误：脱敏后数字看起来"不对"

**这是正常现象！** hash 策略脱敏后的数字会看起来像真实数据，但和原始值完全不同——这正是设计目标。如果你希望脱敏后的数字也"合理"，请使用 blur 策略；如果希望彻底隐藏数值，请使用 placeholder 策略。

---

### 💡 常见踩坑总结

| 踩坑 | 正确做法 |
|------|---------|
| 用 placeholder 脱敏后发给 AI 做数据分析 | placeholder 只保留结构，AI 无法做数值分析。数据分析用 hash 或 blur |
| Excel 脱敏没保存映射表，事后想还原 | 脱敏时务必加 `-m mapping.json` |
| 对 PDF 输出排版期望过高 | PDF 脱敏后保留文字内容和分页，不保留原排版样式 |
| 忘记安装 OCR 依赖就要脱敏图片 | 图片脱敏前先确认已安装 Tesseract OCR 引擎 |
| 对 .doc 旧格式直接脱敏 | 先手动转为 .docx 再脱敏 |

---

## 与其他技能的协作

### 本技能独立处理
- .docx、.pptx、.xlsx、.csv、.md、.txt、.html（简单）、.pdf（文字型）
- .png、.jpg、.gif、.bmp、.webp、.tiff（图片 OCR，需 Tesseract）
- .json、.xml、.yaml 等文本格式（自动回退）

### 委托专项 skill 的场景

| 场景 | 合作 skill | 流程 |
|------|-----------|------|
| 扫描版 PDF（图片型，无文字层） | `pdf` skill | OCR 提取文本 → `mask.py` 脱敏 → .txt 输出 |
| JS 渲染的复杂 HTML（SPA 页面） | `agent-browser` / `playwright-cli` | 浏览器提取可见文本 → `mask.py` 脱敏 → .txt 输出 |
| .doc（旧版 Word） | 用户先转为 .docx | `file_mask.py` 正常处理 |
| .xls（旧版 Excel） | 用户先转为 .xlsx | `dataframe_mask.py` 正常处理 |

---

## 特别说明

- **格式保留**：Word/PPT 脱敏后保留原文档的字体、字号、颜色、表格等所有格式
- **页眉页脚**：Word 文档的页眉页脚中的数字也会被脱敏
- **中文 PDF**：PDF 输出自动查找系统 CJK 字体，支持中文内容
- **电话号码保护**：使用 `--skip-phone` 保留电话号码不被脱敏
- **金额数据**：自动识别带"元""万""亿"等单位的数字
- **负数保护**：负数脱敏后保留负号
- **哈希相对位置**：hash 策略在量级区间内保持相对位置，101 脱敏后仍小于 900
- **映射表**：hash 和 placeholder 策略自动生成映射表，支持完整还原
- **脱敏后需人工复核**：工具不做内容级别的安全判断。脱敏后的数据能否外用（上传大模型、发送第三方等），仍需人工核查
