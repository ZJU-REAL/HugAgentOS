
# Large File Write Skill

## 问题背景

在 AI Agent 模式下，使用 shell here-document 或其他 shell 命令向文件写入大块内容时，经常出现：
- 内容被截断（工具输出超过 token 限制）
- heredoc 内容未生效（zsh 特殊字符转义问题）
- 多次追加导致重复内容或语法错误
- Windows PowerShell 为了转义内容而执行 `Get-Content -Raw | ConvertTo-Json`，会把大 JSX/JS 文件整体读入内存并 JSON 转义，可能导致内存暴涨

## 解决方案

优先使用当前 agent 运行时提供的结构化文件写入工具（如 create_file / Write / file edit tool）直接创建目标文件。内容过大、单次写入可能截断时，再用结构化写入工具创建 Node.js payload 脚本，由 payload 调用 `fs.writeFileSync` 写目标文件。

OpenYida 业务场景下，payload、内容片段和验证脚本必须放在 `<projectRoot>/.cache/openyida/<项目名或任务名>/scripts/`，不要写系统临时目录、仓库根目录或 `.cache/` 顶层。

## 使用方式

三种写入模式的完整代码示例，详见 [references/write-patterns.md](./references/write-patterns.md)：

- **模式一**：创建内容脚本后执行（推荐）— 用结构化文件写入工具创建临时 JS 脚本，再 `node` 执行
- **模式二**：追加内容到已有文件 — 用 `fs.appendFileSync` 追加大块内容
- **模式三**：使用通用写入脚本（content-file 模式）— 内容文件也先由结构化文件写入工具创建

### 快速示例

```js
// <projectRoot>/.cache/openyida/<任务名>/scripts/content-payload.js
const fs = require('fs');
const content = `// 你的大块内容（支持任意长度）
export function myFunction() { ... }
`;
fs.writeFileSync('/path/to/target.js', content, 'utf8');
console.log('写入完成，行数：', content.split('\n').length);
```

```bash
node .cache/openyida/<任务名>/scripts/content-payload.js
wc -l /path/to/target.js   # 验证行数
```

## 核心原则

1. **永远不要用 heredoc 写大文件** — 改用结构化文件写入工具创建临时 JS 脚本
2. **永远不要用 PowerShell JSON 化大文件** — 禁止 `Get-Content -Raw <file> | ConvertTo-Json`、`ConvertFrom-Json` 处理 `.oyd.jsx/.jsx/.js` 页面源码
3. **内容放在 JS 模板字符串里** — 支持任意长度，不受 shell 限制
4. **写完立即验证** — `wc -l` 检查行数，`tail` 检查末尾内容
5. **分段写入大文件** — 超过 300 行的内容，拆分为多个结构化写入 + `node` 执行
6. **本技能不读写 memory**：文件写入为纯本地操作，不依赖跨会话的 memory 状态

## 适用场景

- 宜搭自定义页面代码（通常 500-1500 行）
- Three.js 场景代码
- 任何超过 100 行的代码文件写入
- Windows 环境下写入/修补大 `.oyd.jsx` 页面源码

## Windows / PowerShell 禁止模式

不要为了转义或生成 patch，把大页面源码转换成 JSON 字符串：

```powershell
# 禁止：会整体读入并生成多份转义副本，可能瞬间占用几十 GB 内存
Get-Content -Raw project\pages\src\vendor-section.oyd.jsx | ConvertTo-Json
```

正确做法是直接用 Node 写入目标文件；需要局部替换时，用 Node 读取后做精确字符串/AST 替换并直接 `fs.writeFileSync` 写回，不要额外包成 JSON patch。

## 触发条件

**正向触发**：
- 需要写入超过 100 行的代码文件
- 使用 heredoc 或 shell 命令写文件时出现内容截断
- 写入宜搭自定义页面代码（通常 500-1500 行）

**不适用场景（不要触发）**：
- 文件内容少于 100 行 → 直接使用 `create_file` 工具
- 二进制文件写入 → 不适用
- 仅追加少量内容 → 直接使用 `create_file`

## 异常处理

| 问题 | 原因 | 处理方式 |
|------|------|----------|
| `node: command not found` | Node.js 未安装 | 先安装 Node.js ≥ 16 |
| 脚本执行后文件为空 | 模板字符串语法错误 | 检查反引号是否闭合，特殊字符是否转义 |
| 写入不完整 | 内容超过单次 create_file 限制 | 按「分段写入」方式拆分为多个脚本 |
| 权限拒绝 | 目标路径无写权限 | 检查目标目录权限；OpenYida 业务文件仍应留在 project 工作目录内 |
