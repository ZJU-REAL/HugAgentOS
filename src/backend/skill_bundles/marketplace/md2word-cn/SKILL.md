---
name: md2word-cn
developer: rongyu(荣彧)
description: |
  将 Markdown 文档转换为 Word 文档，专为中文文档设计。支持完整的 Markdown 语法，字体统一使用仿宋。适用于周报、日报、报告等中文办公文档的转换。
---

# md2word-cn

专为中文文档设计的 Markdown 转 Word 转换器。

## 快速开始

```bash
# 安装依赖
pip3 install python-docx

# 转换文件
python3 <skill>/scripts/md2word.py <输入.md> <输出.docx>
```

## 支持的 Markdown 语法

| 语法 | 效果 |
|------|------|
| `# 一级标题` | 居中标题，22pt，加粗 |
| `## 二级标题` | 16pt，加粗 |
| `### 三级标题` | 14pt，加粗 |
| `#### 四级标题` | 12pt，加粗 |
| `**加粗**` | 加粗 |
| `*斜体*` | 斜体 |
| `~~删除线~~` | 删除线 |
| `` `代码` `` | 行内代码 |
| ```` ```代码块``` ```` | 代码块 |
| `[链接](url)` | 链接 |
| `1. 有序列表` | 有序列表（手动编号，避免重复） |
| `- 无序列表` | 无序列表 |
| `> 引用` | 引用 |
| `---` | 水平分割线 |

## 字体格式

- 正文：仿宋 12pt
- 一级标题：仿宋 22pt，居中加粗
- 二级标题：仿宋 16pt 加粗
- 三级标题：仿宋 14pt 加粗
- 四级标题：仿宋 12pt 加粗

## 使用示例

```bash
# 转换周报
python3 scripts/md2word.py weekly_report.md weekly_report.docx

# 转换报告
python3 scripts/md2word.py report.md report.docx
```

## 脚本

- `scripts/md2word.py` - 主转换脚本

## 注意事项

- 有序列表使用手动编号，避免 Word 自动编号导致的重复
- 代码块样式为灰色背景
- 链接自动添加下划线
