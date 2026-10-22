---
name: doc-format-gw
description: 公文排版工具 - 根据公文格式规范自动排版Word文档。适用于发送Word文件前进行格式调整。
metadata:
  {
    "openclaw": {
      "emoji": "📝",
      "requires": { "bins": ["python3"], "env": [] },
      "primaryEnv": ""
    }
  }
---

# 公文排版工具

根据公文格式规范自动排版Word文档。

## 触发条件

用户要求：
- 排版Word文档
- 格式化公文
- 根据公文格式规范排版
- 发送Word前格式化

## 功能

- 自动识别标题层级（一级、二级、三级、四级）
- 设置正文格式（字体、字号、行距、对齐）
- 设置页边距
- 表格格式化
- 页码设置

## 使用方法

```bash
python3 ~/.npm-global/lib/node_modules/openclaw/skills/doc-format-gw/format_gw.py <input.docx> <output.docx>
```

或者在Python中调用：

```python
from format_gw import format_document
format_document('input.docx', 'output.docx')
```

## 格式规范

详见同目录下的 `references/format-rules.md`

## 依赖

- python3
- python-docx
