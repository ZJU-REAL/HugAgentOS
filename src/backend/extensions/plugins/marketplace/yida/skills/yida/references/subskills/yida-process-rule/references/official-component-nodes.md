# 官方组件节点高级透传

宜搭真实流程设计器还支持自动动作节点。OpenYida 对这些节点采用“真实组件 props 透传 + processJson 适配”模式：节点顺序、连线和 `processJson.type` 自动生成，`viewJson.props` 保留真实组件配置，`processJson.props` 按线上 `yida-simple-flow` 的转换规则生成。每个节点的业务配置必须来自真实配置（例如在设计器中配置一次后复制 props，或从已有流程版本中读取）。

## 支持清单

| `componentName` / `type` | `processJson.type` | 典型配置字段 |
| --- | --- | --- |
| `ConnectorNode` / `connector` | `innerConnector` / `thirdConnector` / `httpConnector` / `faasConnector` | `connectorRules` |
| `GroovyNode` / `groovy` | `CodeExecutor` | `groovy` |
| `JavaScriptNode` / `javascript` | `CodeExecutor` | `JavaScript` |
| `GetSingleDataNode` / `getSingleData` | `dataRetrieve` | `getData` |
| `GetBatchDataNode` / `getBatchData` | `dataRetrieve` | `getData` |
| `AddDataNode` / `addData` | `dataCreate` | `addDataRules` |
| `UpdateDataNode` / `updateData` | `dataUpdate` | `updateDataRules` |
| `DeleteDataNode` / `deleteData` | `dataDelete` | `deleteData` |
| `SendMessageNode` / `sendMessage` | `sendMessage` | `sendMessageRules` |
| `SendEmailNode` / `sendEmail` | `sendEmail` | `sendEmailRules` |
| `InitiateApprovalNode` / `subProcess` | `initiateApproval` | `initiateApprovalRules` |
| `SendCardNode` / `CardNode` | `sendCard` | `sendCardRules` / `cardRules` |
| `UpdateCardNode` / `CardUpdateNode` | `updateCard` | `updateCardRules` / `cardUpdateRules` |
| `CycleContainer` / `foreach` | `foreach` | `cycleContainerRules`，也兼容 `cycleRules` / `foreachRules` 别名；`children` |
| `AINode` / `ai` | `AIExecutor` | `workFlowRules` |

## 连接器节点示例

```json
{
  "type": "connector",
  "name": "创建钉钉待办",
  "connectorRules": {
    "connectorId": "G-CONN-xxx",
    "actionId": "G-ACT-xxx",
    "connector": { "mode": 1 },
    "inputs": {
      "assignments": [
        { "column": "subject", "valueType": "literal", "value": "审批待办", "assignments": [] },
        { "column": "creatorId", "valueType": "processVar", "value": "form_inst_modifier", "assignments": [] }
      ]
    }
  }
}
```

`connector.mode` 映射：`1 -> innerConnector`，`3 -> thirdConnector`，`5 -> httpConnector`，`9 -> faasConnector`。保存时 `connectorRules` 会按真实设计器规则转换为 `processJson.props.inputs`；如果租户返回了不同类型，可显式传入 `processType` 覆盖。

## 数据 / 消息 / 代码节点示例

```json
{
  "nodes": [
    {
      "componentName": "GetSingleDataNode",
      "name": "查询客户",
      "getData": {
        "sourceId": "FORM-CUSTOMER",
        "assignments": []
      }
    },
    {
      "componentName": "JavaScriptNode",
      "name": "计算评级",
      "JavaScript": {
        "action": { "code": "return inputs;" },
        "outputs": []
      }
    },
    {
      "componentName": "SendMessageNode",
      "name": "通知负责人",
      "sendMessageRules": {
        "messageType": "workNotice",
        "messageInfo": {
          "title": "流程通知",
          "content": "请及时处理"
        },
        "toUsers": [
          { "userId": "manager001", "userName": "负责人" }
        ]
      }
    }
  ]
}
```

## 透传规则

- `props` 会进入 `viewJson`，其中 `name` / `nodeName` / `description` 自动补齐。
- `processJson.props` 会移除 `name` / `nodeName` / `description` / `title` 等视图字段，并按真实设计器规则转换连接器、代码、取数、新增数据、AI 等节点；如需覆盖可传 `processProps`。
- 官方组件节点可用 `children` / `childNodes` 嵌套子节点，常用于 `CycleContainer`。
- 线上设计器里的“分支节点”不是独立的 `BranchNode` 序列化组件，实际由 `ConditionContainer` + `ConditionNode` / `ParallelNode` 表达；普通条件/并行优先使用主文档中的 `route` / `parallel` DSL。
- 不认识的节点类型会直接报错，不会静默跳过，避免生成断链流程。
- 从零创建复杂集成自动化时优先使用 `yida-integration`；本参考用于流程表单规则里需要高级组件节点、或已有真实节点配置需要固化的场景。
