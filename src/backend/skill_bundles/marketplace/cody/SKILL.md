---
name: code-review-assistant
description: 代码 Review 助手。分析 Git diff 或代码片段，输出结构化中文 Review 报告，覆盖 Bug、安全漏洞、性能问题、可读性、最佳实践、类型安全、错误处理、测试覆盖。支持严格程度配置（信息/优化/标准/严重）和多种主流语言（Python/JS/TS/Java/Go/Rust）。支持 GitHub/GitLab PR diff 获取，支持 Markdown/JSON/HTML 多种输出格式。使用场景：用户说"帮我 review 代码"、"检查这段代码"、"review 一下最近的改动"、"review 这个 PR"、"看看这个 diff 有没有问题"、"代码审查"、"code review"、"严格模式 review"、"快速 review"。
---

# 代码 Review 助手

## 工作流程

### 第一步：获取代码

按优先级尝试以下方式：

1. **PR diff**（最高优先级）：运行 `scripts/get_pr_diff.py`，支持 GitHub/GitLab
2. **Git diff**：运行 `scripts/get_diff.py` 获取本地变更
3. **用户粘贴**：直接分析用户提供的代码片段或 diff 文本
4. **指定文件**：读取用户指定的文件内容

### 第二步：确认严格程度

若用户未指定，默认使用**标准模式**。

| 模式 | 触发词 | 检查范围 |
|---|---|---|
| 🔵 信息 | 快速 review、简单看看 | 命名规范、注释完整性 |
| 🟢 优化 | -（默认最低） | 可读性问题、最佳实践 |
| 🟡 建议 | 标准 review | + 性能问题、明显 Bug |
| 🔴 严重 | 严格模式、PR review | + 安全漏洞、严重 Bug |

详见 `references/severity-guide.md`。

### 第三步：执行分析

按以下维度检查，详细规则见 `references/review-dimensions.md`：

- 🐛 **潜在 Bug** — 空指针、越界、异常处理、类型错误
- 🔒 **安全问题** — SQL 注入、XSS、硬编码密钥、权限校验
- ⚡ **性能问题** — N+1 查询、不必要循环、低效数据结构
- 📖 **可读性** — 过长函数、魔法数字、晦涩命名
- ✅ **最佳实践** — DRY 原则、错误处理一致性
- 🧪 **类型安全** — 类型注解、隐式转换
- 🛡️ **错误处理** — 异常捕获、返回值校验
- 🧪 **测试覆盖** — 关键逻辑缺少测试提示

### 第四步：语言特定规则

根据代码语言加载对应规则：`references/languages/` 目录下包含：

- `python.md` — Python 特定检查
- `javascript.md` — JavaScript/TypeScript 检查
- `go.md` — Go 语言检查
- `java.md` — Java 检查
- `rust.md` — Rust 检查

### 第五步：输出报告

支持三种格式，默认 Markdown：

- **Markdown**（默认）：适合直接阅读和分享
- **JSON**：适合 CI 集成和二次处理，使用 `--format json`
- **HTML**：适合生成可分享的报告，使用 `--format html`

输出模板见 `references/report-template.md`。

## 快速参考

| 用户说 | 对应操作 |
|---|---|
| "review 最近的提交" | `get_diff.py --commits HEAD~1` |
| "review 和 main 的差异" | `get_diff.py --branch main` |
| "review GitHub PR #123" | `get_pr_diff.py --provider github --pr 123` |
| "严格模式 review" | 启用 🔴 严重模式 |
| "快速看看" | 启用 🔵 信息模式 |
| "输出 JSON" | 使用 JSON 模板 |
| "生成 HTML 报告" | 使用 HTML 模板 |

## 注意事项

- 优先给出**可操作的改进建议**，不只是指出问题
- 每条意见附带**具体行号**（如能定位）
- 中文输出，技术术语保留英文原词
- 若 diff 超过 500 行，按文件分批处理，每批处理完询问是否继续
- 使用语言特定规则时，先识别代码语言再加载对应检查项