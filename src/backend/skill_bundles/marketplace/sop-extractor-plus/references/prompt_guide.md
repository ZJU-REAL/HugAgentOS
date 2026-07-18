# SOP 提取器 — AI 对话引导模板

## 使用场景

Skill `sop-extractor` 覆盖以下使用场景：

### 场景 1：用户口述流程
用户说"帮我把刚才说的流程整理成SOP"时：
1. 请用户直接粘贴口述文本或上传转录文件
2. 调用 `sop_extractor.py` 提取结构化SOP
3. 以 Markdown 或 HTML 格式预览结果
4. 告知用户评分和缺口，询问是否需要优化

### 场景 2：用户上传文本文件
用户提供 .txt / .md 文件时：
1. 直接调用 `sop_extractor.py -f <file>`
2. 展示提取结果摘要（步骤数、耗时、复杂度）
3. 自动运行优化分析

### 场景 3：用户已有 SOP 需要优化
用户说"帮我优化这个SOP"时：
1. 运行 `sop_optimizer.py -i <sop.json>`
2. 展示质量评分和改进建议
3. 生成优化后的 SOP

### 场景 4：多格式输出
用户需要特定格式时：
1. Markdown: `sop_formatter.py -i <sop.json> -f markdown`
2. 交互HTML: `sop_formatter.py -i <sop.json> -f html`
3. 检查清单: `sop_formatter.py -i <sop.json> -f checklist`
4. 培训卡片: `sop_formatter.py -i <sop.json> -f training`
5. 流程图: `sop_formatter.py -i <sop.json> -f mermaid`
6. 全部格式: `sop_formatter.py -i <sop.json> -f all -o output_dir/`

### 场景 5：可视化报告
用户需要综合分析报告时：
1. 运行 `report_generator.py -s <sop.json> [--optimization <report.json>]`
2. 自动打开 HTML 预览

## 完整工作流（推荐）

```
用户口述文本
    ↓
sop_extractor.py → sop_output.json
    ↓
sop_optimizer.py → sop_optimized.json + optimization_report.json
    ↓
sop_formatter.py → sop.html + sop.md + checklist + ...
    ↓
report_generator.py → sop_report.html
```

## 对话引导话术

### 第一轮：收集信息
```
好的，我来帮你把流程整理成标准SOP。

请把你的操作流程口述或粘贴过来（越详细越好），比如：
- 第一步做什么
- 用到了什么工具/系统
- 中间需要检查什么
- 最后怎么确认完成
- 每一步大概要花多长时间

也可以直接上传录音转录文本或文字笔记。
```

### 第二轮：展示结果
```
✅ SOP已提取完成！

📊 概览：
- 共 X 个步骤
- 预估总耗时：X
- 复杂度：X
- 发现 X 个检查点、X 处注意事项

需要我帮你进一步优化吗？（比如补充检查点、标注常见错误、生成培训卡片等）
```

### 第三轮：优化分析
```
🔍 优化分析结果：

质量评分：85/100 (A级 - 优秀)
发现 2 个缺口：
- 缺少前置条件说明
- 部分步骤没有时间估算

改进建议：
- 步骤3和步骤4可并行执行
- 步骤5可考虑自动化

要我帮你生成优化版吗？
```

### 第四轮：输出交付
```
📦 已为你生成：
- SOP文档（交互式HTML）
- 执行检查清单（可打印）
- 培训卡片（新人上手用）
- 流程图（Mermaid格式）
- 综合分析报告

你可以直接打开 sop_report.html 查看完整报告。
```

## 注意事项

- 口述文本质量直接影响提取效果，鼓励用户描述得越详细越好
- 对于非常简短的描述（< 50 字），主动追问补充细节
- 优化建议仅供参考，最终由用户确认
- 多个 SOP 可分别提取后统一管理
