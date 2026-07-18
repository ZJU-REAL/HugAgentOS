# yida-process-rule 使用示例

## 示例 1：配置简单两级审批流程

### 场景

为"费用报销"表单配置审批流程：部门主管审批 → 财务审批（金额 > 1000 时）→ 结束。

### 前置步骤：获取字段 ID

```bash
openyida get-schema APP_XXX FORM-EXPENSE
# 从 stdout 中提取字段 ID，并用结构化文件写入工具更新 <projectRoot>/.cache/<项目名>-schema.json：
# 报销金额：numberField_amount
# 报销类型：selectField_expenseType
# 报销说明：textareaField_remark
# 附件：attachmentField_receipt
```

### 使用结构化文件写入工具创建流程定义文件 `<projectRoot>/.cache/openyida/expense/process/expense-process.json`

```json
{
  "nodes": [
    {
      "type": "approval",
      "name": "部门主管审批",
      "approver": "originator",
      "description": "部门主管审核报销申请",
      "formConfig": {
        "behaviorList": [
          { "fieldId": "numberField_amount", "fieldBehavior": "READONLY" },
          { "fieldId": "selectField_expenseType", "fieldBehavior": "READONLY" },
          { "fieldId": "textareaField_remark", "fieldBehavior": "READONLY" },
          { "fieldId": "attachmentField_receipt", "fieldBehavior": "NORMAL" }
        ]
      }
    },
    {
      "type": "route",
      "conditions": [
        {
          "name": "金额超过1000",
          "rules": [
            {
              "fieldId": "numberField_amount",
              "fieldName": "报销金额",
              "op": "GreaterThan",
              "value": "1000",
              "componentType": "NumberField"
            }
          ],
          "childNodes": [
            {
              "type": "approval",
              "name": "财务审批",
              "approver": "originator",
              "description": "财务部审核大额报销",
              "formConfig": {
                "behaviorList": [
                  { "fieldId": "numberField_amount", "fieldBehavior": "READONLY" },
                  { "fieldId": "selectField_expenseType", "fieldBehavior": "READONLY" },
                  { "fieldId": "textareaField_remark", "fieldBehavior": "READONLY" },
                  { "fieldId": "attachmentField_receipt", "fieldBehavior": "NORMAL" }
                ]
              }
            }
          ]
        }
      ]
    }
  ]
}
```

### 执行命令

```bash
openyida configure-process APP_XXX FORM-EXPENSE .cache/openyida/expense/process/expense-process.json
```

### 输出

```json
{
  "success": true,
  "processCode": "TPROC--XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
  "processId": "83145794990",
  "processVersion": 2,
  "appType": "APP_XXX",
  "formUuid": "FORM-EXPENSE"
}
```

---

## 示例 2：带回退规则的质检流程

### 场景

质检流程：取样 → 检验 → 判断结果（合格/不合格）。不合格时跳回检验节点重新检验。

### 流程定义 `<projectRoot>/.cache/openyida/quality/process/quality-process.json`

```json
{
  "nodes": [
    {
      "type": "approval",
      "name": "取样",
      "approver": "originator",
      "formConfig": {
        "behaviorList": [
          { "fieldId": "textField_sampleId", "fieldBehavior": "NORMAL" },
          { "fieldId": "textareaField_sampleNote", "fieldBehavior": "NORMAL" },
          { "fieldId": "textareaField_inspectNote", "fieldBehavior": "HIDDEN" },
          { "fieldId": "radioField_result", "fieldBehavior": "HIDDEN" },
          { "fieldId": "attachmentField_photo", "fieldBehavior": "NORMAL" }
        ]
      }
    },
    {
      "type": "approval",
      "name": "检验",
      "approver": "originator",
      "formConfig": {
        "behaviorList": [
          { "fieldId": "textField_sampleId", "fieldBehavior": "READONLY" },
          { "fieldId": "textareaField_sampleNote", "fieldBehavior": "READONLY" },
          { "fieldId": "textareaField_inspectNote", "fieldBehavior": "NORMAL" },
          { "fieldId": "radioField_result", "fieldBehavior": "HIDDEN" },
          { "fieldId": "attachmentField_photo", "fieldBehavior": "NORMAL" }
        ]
      }
    },
    {
      "type": "approval",
      "name": "判断结果",
      "approver": "originator",
      "formConfig": {
        "behaviorList": [
          { "fieldId": "textField_sampleId", "fieldBehavior": "READONLY" },
          { "fieldId": "textareaField_sampleNote", "fieldBehavior": "READONLY" },
          { "fieldId": "textareaField_inspectNote", "fieldBehavior": "READONLY" },
          { "fieldId": "radioField_result", "fieldBehavior": "NORMAL" },
          { "fieldId": "attachmentField_photo", "fieldBehavior": "NORMAL" }
        ]
      },
      "routeRules": [
        { "when": "disagree", "jumpTo": "检验" }
      ]
    }
  ]
}
```

### 执行命令

```bash
openyida configure-process APP_XXX FORM-QUALITY .cache/openyida/quality/process/quality-process.json
```

---

## 字段权限配置规范

| 节点阶段 | 本节点负责字段 | 前序字段 | 后续字段 |
|---------|-------------|---------|---------|
| 取样节点 | `NORMAL`（可编辑） | 无 | `HIDDEN`（隐藏） |
| 检验节点 | `NORMAL`（可编辑） | `READONLY`（只读） | `HIDDEN`（隐藏） |
| 判断节点 | `NORMAL`（可编辑） | `READONLY`（只读） | 无 |
| 附件字段 | 所有节点均为 `NORMAL` | — | — |

## 常见错误

| 错误 | 原因 | 解决方式 |
|------|------|---------|
| `fieldId 不存在` | 手写猜测了 fieldId | 先执行 `openyida get-schema` 获取真实 fieldId |
| 条件分支报错 | 最后一个 conditionNode 不是 `else` | 确认最后一个条件的 `conditionType` 为 `"else"` |
| 流程发布后未生效 | 只保存未发布 | 确认已执行发布步骤，检查流程版本号 |
| 嵌套分支超过 3 层 | 流程设计过于复杂 | 重新设计流程结构，将复杂条件拆分为多个节点 |

---

## 示例 3：嵌套分支 + 字段权限 + 跳转规则

### 场景

订单处理流程：检查订单 → 订单有效判断 → 库存判断（充足/不足）→ 对应处理。

### 流程定义 `<projectRoot>/.cache/openyida/order/process/order-process.json`

```json
{
  "nodes": [
    {
      "type": "approval",
      "name": "检查订单",
      "approver": "originator",
      "formConfig": {
        "behaviorList": [
          { "fieldId": "textField_xxx", "fieldBehavior": "READONLY" },
          { "fieldId": "radioField_aaa", "fieldBehavior": "NORMAL" }
        ]
      }
    },
    {
      "type": "route",
      "conditions": [
        {
          "name": "订单有效",
          "rules": [
            {
              "fieldId": "radioField_aaa",
              "op": "Equal",
              "value": "有效",
              "componentType": "RadioField",
              "fieldName": "订单是否有效"
            }
          ],
          "childNodes": [
            { "type": "approval", "name": "确认订单", "approver": "originator" },
            {
              "type": "route",
              "conditions": [
                {
                  "name": "库存充足",
                  "rules": [
                    {
                      "fieldId": "selectField_xxx",
                      "op": "Equal",
                      "value": "充足",
                      "componentType": "SelectField",
                      "fieldName": "库存状态"
                    }
                  ],
                  "childNodes": [
                    { "type": "approval", "name": "交付产品", "approver": "originator" }
                  ]
                },
                {
                  "name": "库存不足",
                  "rules": [
                    {
                      "fieldId": "selectField_xxx",
                      "op": "Equal",
                      "value": "不足",
                      "componentType": "SelectField",
                      "fieldName": "库存状态"
                    }
                  ],
                  "childNodes": [
                    { "type": "approval", "name": "采购", "approver": "originator" }
                  ]
                }
              ]
            }
          ]
        }
      ]
    }
  ]
}
```

### 执行命令

```bash
openyida configure-process APP_XXX FORM-ORDER .cache/openyida/order/process/order-process.json
```

---

## 示例 4：配置自定义详情页（解决钉钉工作通知链接问题）

### 场景

配置流程后，钉钉工作通知推送的链接需要指向自定义详情页，而非原生 `taskDetail.htm`。

### 流程定义 `<projectRoot>/.cache/openyida/custom-detail/process/custom-detail-process.json`

```json
{
  "processDetailUrl": "https://mson55.aliwork.com/alibaba/web/APP_XXX/inst/taskDetail.htm?customPage=true&pageId=PAGE-YYY",
  "processMobileDetailUrl": "https://mson55.aliwork.com/alibaba/mobile/APP_XXX/inst/detail/taskDetail/",
  "nodes": [
    {
      "type": "approval",
      "name": "主管审批",
      "approver": "originator"
    }
  ]
}
```

### 执行命令

```bash
openyida configure-process APP_XXX FORM-XXX .cache/openyida/custom-detail/process/custom-detail-process.json
```

### 说明

配置 `processDetailUrl` 后，钉钉工作通知推送的链接将直接指向自定义详情页，而不再是原生 `taskDetail.htm`。

---

## 示例 5：带跳转规则的审批流程

### 场景

审批流程：部门主管审核 → 财务部审核（拒绝时跳回部门主管审核）→ 结束。

### 流程定义 `<projectRoot>/.cache/openyida/jump-rule/process/jump-rule-process.json`

```json
{
  "nodes": [
    {
      "type": "approval",
      "name": "部门主管审核",
      "approver": "originator"
    },
    {
      "type": "approval",
      "name": "财务部审核",
      "approver": "originator",
      "routeRules": [
        { "when": "disagree", "jumpTo": "部门主管审核" }
      ]
    }
  ]
}
```

### 执行命令

```bash
openyida configure-process APP_XXX FORM-XXX .cache/openyida/jump-rule/process/jump-rule-process.json
```

### 流程

`发起 → 部门主管审核 → 财务部审核（拒绝时跳回部门主管审核） → 结束`
