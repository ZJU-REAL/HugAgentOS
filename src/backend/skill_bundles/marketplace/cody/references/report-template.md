# Review 报告模板

## Markdown 格式（默认）

```
## 代码 Review 报告

**模式**：标准 / 宽松 / 严格
**文件数**：X 个文件，+Y 行，-Z 行
**问题汇总**：🔴 严重 N 个 | 🟡 建议 N 个 | 🟢 优化 N 个 | 🔵 信息 N 个

---

### 📄 文件：`path/to/file.py`

#### 🔴 [严重] 第 42 行 — SQL 注入风险

**问题**：用户输入直接拼接进 SQL 查询，存在注入风险。

**当前代码**：
\```python
query = "SELECT * FROM users WHERE name = '" + username + "'"
\```

**建议改为**：
\```python
query = "SELECT * FROM users WHERE name = %s"
cursor.execute(query, (username,))
\```

---

#### 🟡 [建议] 第 78-95 行 — N+1 查询问题

**问题**：在循环内执行数据库查询，当列表较大时性能会显著下降。

**建议**：将查询移到循环外，使用 `IN` 子句批量获取，或使用 ORM 的预加载功能。

---

### 📄 文件：`path/to/another.js`

（同上格式）

---

### ✅ 总体评价

（2-3 句话总结代码质量，指出做得好的地方和最需要优先处理的问题）
```

---

## JSON 格式（CI 集成）

```json
{
  "summary": {
    "mode": "standard",
    "files": 3,
    "added": 150,
    "removed": 30,
    "issues": {
      "critical": 2,
      "warning": 5,
      "info": 3,
      "style": 2
    }
  },
  "files": [
    {
      "path": "src/auth.py",
      "issues": [
        {
          "severity": "critical",
          "line": 42,
          "category": "security",
          "title": "SQL 注入风险",
          "description": "用户输入直接拼接进 SQL 查询",
          "suggestion": "使用参数化查询"
        }
      ]
    }
  ],
  "passed": false,
  "timestamp": "2026-03-19T21:30:00+08:00"
}
```

**使用场景**：
- CI/CD 流水线集成
- 自动化代码质量门禁
- 与其他工具联动

---

## HTML 格式（分享报告）

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>代码 Review 报告</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; }
    .summary { background: #f5f5f5; padding: 15px; border-radius: 8px; margin-bottom: 20px; }
    .file { margin-bottom: 30px; }
    .issue { border-left: 4px solid #ccc; padding: 10px 15px; margin: 10px 0; }
    .issue.critical { border-color: #dc3545; background: #fff5f5; }
    .issue.warning { border-color: #ffc107; background: #fffdf5; }
    .issue.info { border-color: #17a2b8; background: #f5fcff; }
    .issue.style { border-color: #6c757d; background: #f9f9f9; }
    code { background: #f0f0f0; padding: 2px 6px; border-radius: 4px; }
    pre { background: #2d2d2d; color: #f8f8f2; padding: 15px; border-radius: 8px; overflow-x: auto; }
  </style>
</head>
<body>
  <h1>📋 代码 Review 报告</h1>
  
  <div class="summary">
    <strong>模式：</strong>标准<br>
    <strong>文件数：</strong>3 个文件，+150 行，-30 行<br>
    <strong>问题汇总：</strong>
    <span style="color:#dc3545">🔴 严重 2</span> |
    <span style="color:#ffc107">🟡 建议 5</span> |
    <span style="color:#28a745">🟢 优化 3</span>
  </div>

  <div class="file">
    <h2>📄 src/auth.py</h2>
    
    <div class="issue critical">
      <h3>🔴 [严重] 第 42 行 — SQL 注入风险</h3>
      <p><strong>问题：</strong>用户输入直接拼接进 SQL 查询，存在注入风险。</p>
      <pre><code>query = "SELECT * FROM users WHERE name = '" + username + "'"</code></pre>
      <p><strong>建议：</strong>使用参数化查询</p>
    </div>
  </div>
</body>
</html>
```

---

## 无问题时的格式

### Markdown
```
## 代码 Review 报告

**模式**：标准
**文件数**：X 个文件

✅ 未发现明显问题。代码结构清晰，逻辑正确，符合最佳实践。
```

### JSON
```json
{
  "summary": { "files": 3, "issues": { "critical": 0, "warning": 0, "info": 0, "style": 0 } },
  "passed": true
}
```

---

## 注意事项

- 严重问题必须给出**具体修改建议**，不能只说"有问题"
- 建议和优化级别可以只描述方向，不强制给出代码示例
- 总体评价要**先肯定再建议**，保持建设性语气
- 若同一类问题出现多次，合并描述，不要重复列举
- JSON 格式中 `passed: false` 表示存在严重问题，可用于 CI 门禁