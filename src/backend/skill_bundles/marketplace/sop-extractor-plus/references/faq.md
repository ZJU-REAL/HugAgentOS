# SOP 提取器 — 常见问题解答（FAQ）

共14题，覆盖常见问题、边缘场景、脚本报错处理和工具选择建议。

---

## 基础使用问题

### Q1：我只有几句话的简短描述，能用吗？

**可以**，但输出质量会受限。Skill 不会直接报错，而是启动追问机制：
1. 先展示已识别的框架
2. 主动追问3个关键缺失项（前置条件/工具说明/结束标志）
3. 用户补充后重新提取

> **建议**：描述越详细，SOP 越完整。50字以上描述即可得到基本可用的结果，200字以上可达A级质量。

---

### Q2：我的口述比较混乱，步骤顺序有点乱，会出错吗？

不会出错，Skill 会：
1. 自动识别步骤的逻辑依赖关系，尝试重排顺序
2. 在输出中标注「⚠️ 步骤顺序已调整」
3. 展示理解确认行，让用户确认顺序是否正确

即使描述混乱，也会优先生成可用的框架，而不是拒绝处理。

---

### Q3：我的流程有很多分支和判断条件，能处理吗？

能处理，但有限制：
- **决策点识别**：可识别"如果...则...否则..."等语言模式
- **Mermaid 流程图**：最适合展示有分支的流程（`-f mermaid`）
- **限制**：超过3层嵌套的复杂分支树，建议人工拆分为子流程再分别提取

---

### Q4：跨多个部门的协同流程能处理吗？

可以提取主流程框架，但注意：
- 跨部门交接点（handoff）会被标注为「⚠️ 需人工确认职责边界」
- 各部门的内部子流程建议分开提取，再由人工整合
- 专项引导请参考 `references/collaboration_guide.md`（含角色划分模板）

---

## 输出与格式问题

### Q5：一定要运行多个脚本吗？有没有简单点的方式？

有，使用一键运行脚本：
```bash
python run_all.py -f input.txt -o output_dir/
```
或直接传文本：
```bash
python run_all.py -t "你的流程描述..." -o output_dir/ --format all
```
`run_all.py` 自动串联全部步骤，出错时自动输出修复建议，输出目录包含全部格式。

---

### Q6：流程图（Mermaid 格式）在哪里预览？

Mermaid 是文本格式，有3种预览方式：
1. **在线**：复制内容粘贴到 [mermaid.live](https://mermaid.live)
2. **VS Code**：安装 Mermaid Preview 插件
3. **直接嵌入 HTML**：Skill 生成的 `sop.html` 已自动渲染流程图

---

### Q7：我想要的不是技术文档风格，能生成更简洁的新人培训版吗？

可以，使用 training 格式：
```bash
python sop_formatter.py -i sop.json -f training -o training_card.html
```
培训卡片特点：
- 每步骤只保留核心动作（去掉技术细节）
- 突出注意事项和检查点
- 新人友好排版（大字体/颜色标注）

---

## 脚本报错处理

### Q8：运行 sop_extractor.py 报错 "No steps found"，怎么办？

原因：输入文本中没有被识别为步骤的内容（通常因为描述太抽象）。

解决方案：
1. 检查输入是否包含具体动作词（点击/填写/发送/检查等）
2. 补充更具体的操作描述
3. 降低步骤识别阈值：
```bash
python sop_extractor.py -f input.txt --min-steps 2
```

---

### Q9：生成的HTML报告打开是空白或乱码，怎么办？

**空白：**
```bash
# 确认JSON路径正确（用绝对路径）
python report_generator.py -s "C:/sop/sop_output.json" -o report.html
```

**乱码：**
- 文本文件需保存为 UTF-8 编码（非 GBK）
- Windows 记事本 → 另存为 → 编码选 UTF-8

**其他浏览器显示异常：**
- HTML报告依赖现代浏览器，推荐用 Chrome / Edge

---

### Q10：生成的步骤数量太少（比如只有2-3步），但我描述了很多内容

可能原因：
1. 描述里用了太多连词（"然后"、"接着"）而没有明确分行
2. 多个操作被识别为同一步骤

解决方案：
```bash
# 降低步骤合并阈值，允许更细粒度的步骤
python sop_extractor.py -f input.txt --granularity fine
```

---

### Q11：run_all.py 提示某步骤失败后，如何手动重试单个步骤？

```bash
# 单独重试步骤1（提取）
python sop_extractor.py -f input.txt -o output/sop_output.json

# 单独重试步骤2（优化）
python sop_optimizer.py -i output/sop_output.json -o output/sop_optimized.json --report output/opt.json

# 单独重试步骤3（格式化，以html为例）
python sop_formatter.py -i output/sop_optimized.json -f html -o output/sop.html --theme dark

# 单独重试步骤4（报告）
python report_generator.py -s output/sop_output.json --optimization output/opt.json -o output/sop_report.html
```

---

## 边缘场景与工具选择

### Q12：我的流程涉及微信、企业微信、钉钉等国内工具，能正确识别工具名称吗？

可以。Skill 内置了国内常用软件的识别词库，包括：
- 即时通讯：微信、企业微信、钉钉、飞书
- 办公协同：腾讯文档、金山文档、石墨文档、语雀
- 项目管理：TAPD、JIRA、禅道
- 数据工具：Excel、WPS、腾讯表格

识别后会在步骤中自动标注工具名称，无需额外配置。

---

### Q13：可以同时处理多个SOP文档吗？

当前版本每次处理一个流程，但支持批量化思路：
```bash
# 对多个文件分别提取
for f in *.txt; do
    python sop_extractor.py -f "$f" -o "output_${f%.txt}.json"
done
```
或使用 AI 模式，一次对话中多次调用（每个流程说"第一个流程：..."、"第二个流程：..."），分别生成 JSON 后统一格式化输出。

---

### Q14：什么情况下应该换别的工具，而不用 SOP 提取器？

以下场景换工具效果更好：

| 情况 | 更合适的方案 |
|------|------------|
| 已有标准格式的 Word/Excel SOP，只需排版 | 直接用 Word/WPS 的样式模板 |
| 需要画精确的泳道图（含审批流、系统边界） | 用 Visio / ProcessOn / draw.io |
| 需要和 Jira/TAPD 工单系统打通 | 用项目管理工具自带的流程模块 |
| 流程超过 30 步且涉及5个以上部门 | 先人工拆分为子流程，再逐个提取 |
| 需要生成法律合规级别的操作规程 | 需专业人工起草，AI 仅辅助初稿 |
