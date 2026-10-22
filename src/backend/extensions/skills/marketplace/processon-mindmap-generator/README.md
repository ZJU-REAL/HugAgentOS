# processon-mindmap-generator

用 ProcessOn 生成精美、可继续编辑的思维导图，支持自然语言、长文本和 Markdown 内容。

## 安装与首要配置

### 1. 安装技能
```bash
npx skills add https://github.com/processonai/processon-skills.git --skill processon-mindmap-generator
```

#### 更新技能
```bash
npx skills add https://github.com/processonai/processon-skills.git --skill processon-mindmap-generator --force -g -y
```

## 支持的场景

- 头脑风暴
- 任务拆解
- 问题分析
- 工作总结
- 读书笔记
- 课程梳理
- 写作提纲
- 会议记录
- 知识管理

## 支持的结构

- `mind_free`：思维导图 / 中心放射 / 默认结构
- `mind_right`：逻辑图 / 向右延伸
- `mind_org`：组织结构图
- `mind_ishikawa_left`：鱼骨图
- `mind_timeline_h`：时间轴
- `mind_tree_free`：树形图
- `mind_treeTable_left_title`：树形表格 / 表格图

如果无法明确判断应使用哪一种结构，默认使用 `mind_free`。

## 提示词示例

```text
生成一份《国富论》读书笔记思维导图，突出核心观点、关键概念和阅读收获
```

```text
围绕“开一家咖啡店”做头脑风暴，输出适合创业讨论的思维导图
```

```text
根据这份会议纪要整理思维导图，突出结论、待办和责任人
```

```text
把这篇长文整理成学习笔记型思维导图，保持层级清晰，内容精简
```

## 输出

- Markdown 形式的思维导图正文
- 图片链接
- 在线查看链接

## 本地开发

如果你想在本地迭代这个 skill，而不是从 GitHub 安装，可以把 skill 目录放到本地 skills 目录，例如：

```text
~/.agents/skills/processon-mindmap-generator
```
