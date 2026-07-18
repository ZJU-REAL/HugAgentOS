# AI 自动生成流程定义规范

本文档描述 AI 根据用户需求（如 PRD 文档、流程图、口头描述）生成流程定义 JSON 时必须遵守的规范。

## 🔐 自动生成字段权限（formConfig.behaviorList）

### 核心原则

每个审批节点只允许编辑与其职责直接相关的字段，其他字段设为只读或隐藏。

### 判断规则

#### 1. 识别节点职责

根据节点名称和描述，判断该节点负责填写/操作哪些字段：
- 例如：节点名为「首件取样」→ 负责填写「首件取样记录」
- 例如：节点名为「数据分析判断」→ 负责填写「首件检验结论」
- 例如：节点名为「抽样检查」→ 负责填写「抽样检查记录」和「抽样检验结论」

#### 2. 设置字段行为

- **NORMAL（可编辑）**：该节点职责范围内需要填写的字段
- **READONLY（只读）**：前序节点已填写的字段（需要查看但不能修改）
- **HIDDEN（隐藏）**：后续节点才需要填写的字段（当前阶段无需展示）
- **附件字段**：通常在所有节点都设为 NORMAL（允许随时补充附件）
- **流水号/编号字段**：通常在所有节点都设为 READONLY

#### 3. 生成时机

当表单有 **3 个以上字段** 且流程有 **2 个以上审批节点** 时，必须自动生成字段权限配置。

#### 4. 完整性要求

`behaviorList` 必须包含表单中的**所有字段**，不能遗漏。未明确归属的字段默认设为 READONLY。

### 示例

一个有「取样」→「检验」→「判断」三个节点的流程：

```json
{
  "type": "approval",
  "name": "取样",
  "approver": "originator",
  "formConfig": {
    "behaviorList": [
      { "fieldId": "textField_xxx", "fieldBehavior": "READONLY" },
      { "fieldId": "textareaField_sample", "fieldBehavior": "NORMAL" },
      { "fieldId": "textareaField_inspect", "fieldBehavior": "HIDDEN" },
      { "fieldId": "radioField_result", "fieldBehavior": "HIDDEN" },
      { "fieldId": "attachmentField_xxx", "fieldBehavior": "NORMAL" }
    ]
  }
}
```

```json
{
  "type": "approval",
  "name": "检验",
  "approver": "originator",
  "formConfig": {
    "behaviorList": [
      { "fieldId": "textField_xxx", "fieldBehavior": "READONLY" },
      { "fieldId": "textareaField_sample", "fieldBehavior": "READONLY" },
      { "fieldId": "textareaField_inspect", "fieldBehavior": "NORMAL" },
      { "fieldId": "radioField_result", "fieldBehavior": "HIDDEN" },
      { "fieldId": "attachmentField_xxx", "fieldBehavior": "NORMAL" }
    ]
  }
}
```

```json
{
  "type": "approval",
  "name": "判断",
  "approver": "originator",
  "formConfig": {
    "behaviorList": [
      { "fieldId": "textField_xxx", "fieldBehavior": "READONLY" },
      { "fieldId": "textareaField_sample", "fieldBehavior": "READONLY" },
      { "fieldId": "textareaField_inspect", "fieldBehavior": "READONLY" },
      { "fieldId": "radioField_result", "fieldBehavior": "NORMAL" },
      { "fieldId": "attachmentField_xxx", "fieldBehavior": "NORMAL" }
    ]
  }
}
```

## 🔄 自动识别回退/循环场景并生成跳转规则（routeRules）

### 核心原则

当流程中存在「不合格→重新处理」「退回→重新提交」等回退/循环语义时，自动配置跳转规则。

### 识别规则

#### 1. 关键词识别

在流程描述、节点名称、条件分支名称中识别以下语义：
- **回退语义**：不合格、退回、驳回、返工、重做、重新、再次、整改、修正
- **循环语义**：循环检验、反复审核、多次提交、迭代

#### 2. 跳转目标判断

回退通常跳转到**产生问题的环节**或**需要重新执行的环节**：
- 例如：「不合格→异常处理→再次取样」中，「再次取样」拒绝时应跳回「检验」节点
- 例如：「抽样不合格→异常分析」中，「异常分析」拒绝时应跳回「抽样检查」节点

#### 3. 跳转动作

- `"when": "disagree"` — 审批人点击「拒绝」时触发跳转
- `"jumpTo": "<目标节点名称>"` — 跳转到指定节点重新执行

#### 4. 生成时机

当流程中出现以下模式时，必须自动生成跳转规则：
- 条件分支中有「不合格/不通过」分支，且该分支的末尾节点语义上需要回到前序节点
- 节点名称包含「再次」「重新」「返工」等回退语义
- 用户描述中明确提到「循环」「回退」「退回重审」等需求

#### 5. 常见回退模式

```
模式 A：不合格 → 异常处理 → 重新检验（跳回检验节点）
模式 B：审核不通过 → 退回修改（跳回提交节点）
模式 C：验收不合格 → 整改（跳回整改前的节点）
```

### 示例

```json
{
  "type": "approval",
  "name": "再次取样",
  "approver": "originator",
  "description": "异常处理后重新取样检验",
  "routeRules": [
    { "when": "disagree", "jumpTo": "检验" }
  ]
}
```

```json
{
  "type": "approval",
  "name": "异常分析检验（抽样）",
  "approver": "originator",
  "description": "抽样不合格，进行异常分析",
  "routeRules": [
    { "when": "disagree", "jumpTo": "抽样检查" }
  ]
}
```

## 📋 AI 生成流程定义的完整检查清单

在生成流程定义 JSON 前，AI 必须逐项检查：

| # | 检查项 | 说明 |
|---|--------|------|
| 1 | 每个审批节点是否配置了 `formConfig.behaviorList` | 当表单字段 ≥ 3 且审批节点 ≥ 2 时必须配置 |
| 2 | `behaviorList` 是否包含所有表单字段 | 不能遗漏任何字段 |
| 3 | 每个节点的可编辑字段是否与其职责匹配 | 节点只能编辑自己负责的字段 |
| 4 | 前序已填写字段是否设为 READONLY | 防止后续节点篡改前序数据 |
| 5 | 后续节点字段是否设为 HIDDEN | 避免展示无关信息 |
| 6 | 是否存在回退/循环场景 | 检查条件分支中的「不合格」分支和节点名称中的回退语义 |
| 7 | 回退节点是否配置了 `routeRules` | 跳转目标是否正确指向需要重新执行的节点 |
| 8 | 附件字段是否在所有节点都可编辑 | 附件通常允许随时补充 |
