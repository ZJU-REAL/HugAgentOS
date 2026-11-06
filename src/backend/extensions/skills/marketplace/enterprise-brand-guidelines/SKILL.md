---
name: enterprise-brand-guidelines
description: 企业视觉规范（VI）：把本单位的 logo、品牌色、中英文字体、文案语气和禁用规则记录成一份 brand.json，校验品牌色作为文字时的对比度并自动给出达标的替代色，导出 CSS 变量、主题文件和图表配色，让 PPT、Word、网页、架构图、海报都按同一套规范出品。当用户说"按我们公司的 VI 做""用我们的品牌色""建一份视觉规范""统一一下公司物料风格"，或其他设计技能需要品牌信息时使用。
---

# 企业视觉规范

下文 `scripts/`、`templates/` 指本技能目录 `{baseDir}/scripts/`、`{baseDir}/templates/`。

## 规范文件放在哪里

- 每家单位一份 `brand/brand.json`，和 logo 文件一起放在用户的项目工作区或团队共享空间（如 `brand/logo.svg`）。
- 开始任何品牌相关的设计前，先找这份文件：先看当前项目的 `brand/` 目录，再问用户是否已有规范文件或 VI 手册。
- **找不到就不要猜品牌色**：先按第 1 步建立，或请用户确认使用中性默认风格。

## 第 1 步：建立规范（只做一次）

以 `{baseDir}/templates/brand.json` 为模板，向用户收集信息后写入 `brand/brand.json`：

- 用户有 VI 手册（PDF、PPT）时，读手册提取：标准色（以手册标注的色值为准，CMYK / Pantone 换算成 HEX 时注明是换算值）、标准字体、logo 使用规范、禁用示例。
- 用户只有官网时，可以用 `web_fetch` 读取官网样式提取主色，**提取结果要请用户确认**，网页用色不一定等于标准色。
- 字体要写成字体栈：首选字体在前，后面跟系统可用的替代字体，最后是通用字体族。正式公文场景的标题字体可用方正小标宋，正文用仿宋。
- 不要编造色值、字体或规范条款；不知道的项留空并告诉用户。

## 第 2 步：校验并生成设计令牌

```bash
python3 scripts/brand_tokens.py brand/brand.json -o brand/tokens
```

输出到 `brand/tokens/`：

| 文件 | 用途 |
|---|---|
| `tokens.css` | 网页、HTML 原型、HTML 幻灯片直接引用的 CSS 变量 |
| `tokens.json` | 每个品牌色对底色的对比度、作文字时的替代色、做底色时上面用什么颜色的字 |
| `theme.md` | 与 `theme-factory` 主题文件同格式，可直接当作主题使用 |
| `diagram-roles.md` | `diagram-design` 的配色映射 |

**对比度提醒要转告用户**：很多品牌色（尤其黄色、橙色、浅蓝）直接用作文字会看不清。脚本会给出同色相加深后的替代色，
品牌原色继续用于大面积色块、图形和 logo，文字改用替代色。这一点要向用户说明，不要擅自改动品牌原色。

## 第 3 步：应用到各类产出

| 产出 | 做法 |
|---|---|
| 网页、HTML 原型、HTML 幻灯片（`huashu-design`、`frontend-design`） | 引用 `tokens.css`，字体用 `--brand-font-*` |
| 架构图、流程图（`diagram-design`） | 按 `diagram-roles.md` 的映射保存为 diagram-design 配置文件 |
| PPT、Word、Excel（officecli 系列技能） | 主题色、标题字体、正文字体取 `tokens.json` 和 `brand.json` |
| 海报、视觉作品（`canvas-design`） | 主色、强调色、字体取 `brand.json`，logo 按 `logo` 一节的留白和最小尺寸使用 |
| 统一主题（`theme-factory`） | 直接使用 `theme.md` |

所有产出都要遵守 `brand.json` 中的 `voice`（文案语气）和 `dont`（禁用规则）。
交付时说明用了哪些品牌元素；如果为了可读性用了替代色，也要说明。
