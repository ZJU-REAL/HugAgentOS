# 集成&自动化节点 JSON 结构详解

> 本文档是 `yida-integration` 技能的参考文档，描述各节点的完整 JSON 结构。
> 编写逻辑流定义前必须参考本文档，节点格式复杂且易出错。

## 节点类型速查

| 节点名称 | `json` 中的 `type` | `viewJson` 中的 `componentName` |
| --- | --- | --- |
| 触发节点 | `trigger` | `StartNode` |
| 消息通知节点 | `sendMessage` | `SendMessageNode` |
| 新增数据节点 | `dataCreate` | `AddDataNode` |
| 获取单条数据节点 | `dataRetrieve` | `GetSingleDataNode` |
| 更新数据节点 | `dataUpdate` | `UpdateDataNode` |
| 条件分支容器 | `route` | `ConditionContainer` |
| 条件分支子节点 | `condition` | `ConditionNode` |
| 结束节点 | `finish` | `EndNode` |

## 典型链路示例

**最简通知流（3 个节点）：**
```
trigger → sendMessage → finish
```

**跨表查询后通知（4 个节点）：**
```
trigger → dataRetrieve → sendMessage → finish
```

**复杂流（含新增、查询、条件分支、更新/新增）：**
```
trigger
  → sendMessage（通知）
  → dataCreate（新增表单B数据）
  → dataRetrieve（获取表单C单条数据）
  → route（条件分支）
      ├─ condition（条件1：C有数据）→ dataUpdate（更新C的字段）→ finish
      └─ condition（其他情况，isDefault=true）→ dataCreate（新增C数据）→ finish
```

---

## 各节点 JSON 结构详解

### 触发节点（trigger）

```json
{
  "type": "trigger",
  "nodeId": "node_xxx",
  "nextId": ["node_yyy"],
  "props": {
    "inputs": {
      "formEventType": ["insert"],
      "formUuid": "FORM-XXX",
      "conditions": null,
      "activityAction": [],
      "triggerFormEventRecursively": true
    },
    "triggerType": "FormEvent"
  }
}
```

- `formEventType`：触发事件数组，可选值：`insert`（新增）、`update`（修改）、`delete`（删除）、`comment`（评论）
- `triggerFormEventRecursively`：固定 `true`

---

### 消息通知节点（sendMessage）

```json
{
  "type": "sendMessage",
  "nodeId": "node_xxx",
  "props": {
    "messageType": "NORMAL",
    "messageInfo": {
      "title": "标题：#{textField_abc-TextField}#",
      "content": "内容：#{textField_xyz-TextField}#",
      "buttons": [
        {
          "name": "查看详情",
          "type": "commit",
          "value": "//yidalogin.aliwork.com/{appType}/formDetail/{formUuid}?formInstId=${formInstId}",
          "buttonUuid": "button-XXXX"
        }
      ]
    },
    "appType": "APP_XXX",
    "toRoles": [],
    "toUsers": [
      { "userId": "0162193625672514", "userName": "张三" }
    ],
    "userFields": ["employeeField_abc"]
  }
}
```

- `toUsers`：指定接收人（userId + userName），可为空数组
- `userFields`：引用触发表单中的成员字段 ID，该字段的值也会收到通知
- 标题/内容中引用字段变量格式：`#{fieldId-ComponentType}#`（注意带组件类型后缀）
- 按钮 URL 中用 `${formInstId}` 引用当前触发的表单实例 ID

---

### 新增数据节点（dataCreate）

```json
{
  "type": "dataCreate",
  "nodeId": "node_xxx",
  "props": {
    "formUuid": "FORM-目标表单",
    "appType": "APP_XXX",
    "subFormUuid": "",
    "insertType": "form",
    "type": "single",
    "sourceId": "",
    "assignments": [
      {
        "column": "textField_目标字段",
        "valueType": "processVar",
        "value": "textField_触发表单字段",
        "assignments": []
      },
      {
        "column": "textareaField_目标字段",
        "valueType": "column",
        "value": "CONCATENATE(#{textField_a},#{textField_b})",
        "assignments": []
      },
      {
        "column": "numberField_目标字段",
        "valueType": "literal",
        "value": 0,
        "assignments": []
      }
    ]
  }
}
```

---

### 获取单条数据节点（dataRetrieve）

```json
{
  "type": "dataRetrieve",
  "nodeId": "node_xxx",
  "props": {
    "type": "single",
    "filterType": "condition",
    "sort": { "type": "none", "column": "" },
    "sourceId": "FORM-目标表单",
    "appType": "APP_XXX",
    "originalType": "form",
    "subSourceId": "",
    "condition": {
      "condition": "AND",
      "rules": [
        {
          "id": "employeeField_目标表单字段",
          "op": "等于任意一个",
          "opCode": "In",
          "value": "employeeField_触发表单字段",
          "componentType": "EmployeeField",
          "extValue": "processVar",
          "ruleValue": "employeeField_触发表单字段",
          "valueType": "processVar",
          "multiple": false,
          "ruleType": "rule_text"
        }
      ],
      "conditionCode": "&&"
    },
    "quantity": "1",
    "assignments": []
  }
}
```

- `quantity: "1"` 表示只取一条
- 过滤条件中 `extValue: "processVar"` 表示右侧值来自触发表单的流程变量
- 该节点的 `nodeId`（如 `node_ocmmzsdai63`）在后续节点中通过 `${nodeId}.fieldId` 引用其查询结果

---

### 更新数据节点（dataUpdate）

```json
{
  "type": "dataUpdate",
  "nodeId": "node_xxx",
  "props": {
    "type": "node",
    "sourceId": "node_上游dataRetrieve节点ID",
    "subSourceId": "",
    "condition": {},
    "subCondition": {},
    "assignments": [
      {
        "column": "numberField_目标字段",
        "valueType": "column",
        "value": "${node_上游节点ID}.numberField_目标字段+1",
        "__display": "获取单条数据.通知次数+1",
        "__source": "#{node_上游节点ID//numberField_目标字段}+1"
      }
    ],
    "noneOperation": "ignored",
    "rulesFilter": [],
    "tableRulesFilter": []
  }
}
```

- `type: "node"` 表示更新来源是上游节点查询到的那条记录（无需额外过滤条件）
- `sourceId` 填写上游 `dataRetrieve` 节点的 `nodeId`
- `__display` 和 `__source` 仅用于前端展示，不影响执行逻辑

---

### 条件分支节点（route + condition）

条件分支由一个 `route` 容器节点 + 多个 `condition` 子节点组成：

```json
{
  "type": "route",
  "nodeId": "node_route",
  "nextId": ["node_cond1", "node_cond2"],
  "props": { "outgoingType": "priority" },
  "childNodes": [
    {
      "type": "condition",
      "nodeId": "node_cond1",
      "prevId": "node_route",
      "nextId": ["node_操作节点"],
      "props": {
        "priority": 1,
        "isDefault": false,
        "conditions": {
          "condition": "AND",
          "rules": [
            {
              "id": "${node_上游节点ID}.employeeField_字段",
              "op": "有值",
              "opCode": "ExistValue",
              "componentType": "EmployeeField",
              "extValue": "value",
              "valueType": "literal",
              "multiple": false,
              "ruleType": "rule_text"
            }
          ],
          "conditionCode": "&&"
        },
        "calculate": "condition"
      },
      "childNodes": [
        { "type": "dataUpdate", "nodeId": "node_操作节点" }
      ]
    },
    {
      "type": "condition",
      "nodeId": "node_cond2",
      "prevId": "node_route",
      "nextId": ["node_操作节点2"],
      "props": {
        "priority": 2147483647,
        "isDefault": true
      },
      "childNodes": [
        { "type": "dataCreate", "nodeId": "node_操作节点2" }
      ]
    }
  ]
}
```

- `outgoingType: "priority"`：按优先级从上到下匹配，第一个满足条件的分支执行
- `priority: 1`：最高优先级（数字越小越优先）
- `isDefault: true` + `priority: 2147483647`：默认分支（其他情况），兜底执行
- 条件中引用上游节点数据：`"id": "${nodeId}.fieldId"`
- 常用 `opCode`：`ExistValue`（有值）、`NotExistValue`（无值）、`Equal`（等于）、`In`（等于任意一个）

---

## 字段赋值 valueType 规律总结

在 `assignments`（字段赋值）中，`valueType` 决定了 `value` 的解析方式：

| `valueType` | 含义 | `value` 格式示例 |
| --- | --- | --- |
| `processVar` | 引用触发表单的字段值 | `"textField_mmq4ldti"`（直接写字段 ID） |
| `literal` | 固定值（字符串/数字） | `"固定文本"` 或 `0` |
| `column` | 公式计算 | `"CONCATENATE(#{fieldId_a},#{fieldId_b})"` |
| `column` | 引用上游节点字段值 | `"${nodeId}.fieldId"` |
| `column` | 引用上游节点字段并运算 | `"${nodeId}.numberField_xxx+1"` |

> ⚠️ 公式中引用字段变量格式为 `#{fieldId}`（不带组件类型），与消息通知中的 `#{fieldId-ComponentType}#` 不同。

---

## 变量引用格式对照

| 使用场景 | 格式 | 示例 |
| --- | --- | --- |
| 消息通知标题/内容 | `#{fieldId-ComponentType}#` | `#{textField_abc-TextField}#` |
| 公式中引用触发表单字段 | `#{fieldId}` | `#{textField_abc}` |
| 赋值中引用触发表单字段 | `processVar` + `fieldId` | `valueType: "processVar", value: "textField_abc"` |
| 赋值中引用上游节点字段 | `column` + `${nodeId}.fieldId` | `"${node_xxx}.numberField_abc"` |
| 条件中引用上游节点字段 | `${nodeId}.fieldId`（作为 `id`） | `"id": "${node_xxx}.employeeField_abc"` |

---

## 接口说明

### saveProcess（保存 / 发布逻辑流）

> 保存和发布使用**同一个接口**，通过 `isOnline` 参数区分。

- **地址**：`POST /alibaba/web/{appType}/query/simpleProcess/saveProcess.json`
- **Content-Type**：`application/x-www-form-urlencoded`
- **参数**：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `_csrf_token` | String | 是 | CSRF Token |
| `formUuid` | String | 是 | 关联表单 UUID |
| `isLogic` | String | 是 | 固定 `"true"` |
| `isOnline` | String | 是 | `"false"`=保存草稿，`"true"`=发布生效 |
| `json` | String (JSON) | 是 | 节点定义 JSON 字符串 |
| `viewJson` | String (JSON) | 是 | 画布 Schema JSON 字符串 |
| `processCode` | String | 是 | 逻辑流唯一标识，格式 `LPROC-xxx` |
| `needReportLine` | String | 是 | 固定 `"y"` |

- **返回值**：`{ "success": true }`

---

### listLogicflows（查询逻辑流列表）

- **地址**：`GET /alibaba/web/{appType}/query/appLogicflowBinding/listflow.json`
- **Query 参数**：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `appType` | String | 是 | 应用 ID |
| `key` | String | 否 | 按名称关键字模糊搜索 |
| `formUuid` | String | 否 | 按触发表单 UUID 过滤 |
| `status` | String | 否 | `y`=开启，`n`=关闭，空=全部 |
| `pageIndex` | Number | 是 | 页码，从 1 开始 |
| `pageSize` | Number | 是 | 每页条数，**最大 10** |
| `type` | String | 是 | 触发类型：`1`=表单事件，`2`=应用事件，`3`=定时，`5`/`6`=手动触发，前端手动触发页可能传 `5,6` |

> ⚠️ `totalCount` 是表单分组数（不是自动化总数），每个分组内的 `flowList` 才是具体的自动化列表。
> ⚠️ 应用级列表对同一表单只返回首批 `flowList`；当分组 `hasMore=true` 时，需要继续调用 `/query/formLogicflowBinding/listflow.json` 拉取该表单下的剩余自动化。

---

### switchLogicflow（开启 / 关闭逻辑流）

- **地址**：`POST /alibaba/web/{appType}/query/formLogicflowBinding/switchflow.json`
- **Content-Type**：`application/x-www-form-urlencoded`
- **参数**：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `_csrf_token` | String | 是 | CSRF Token |
| `processCode` | String | 是 | 逻辑流唯一标识（`LPROC-xxx` 格式） |
| `formUuid` | String | 是 | 触发表单 UUID |
| `enable` | String | 是 | `"y"`=开启，`"n"`=关闭 |
| `type` | String | 是 | 固定 `"1"` |

> ⚠️ 若目标逻辑流已处于目标状态，接口仍返回 `success: true`，不会报错。

---

### listLogicflowLogs（查询运行日志）

- **地址**：`GET /alibaba/web/{appType}/query/formLogicflowBinding/listLog.json`
- **Query 参数**：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `processCode` | String | 是 | 逻辑流唯一标识（`LPROC-xxx` 格式） |
| `dateType` | String | 否 | 前端默认 `modifyTime`，表示按完成时间筛选；也支持 `startTime` |
| `status` | String | 否 | `3`=执行成功，`2`=执行异常，`0`=执行中；筛选“执行异常”时传 `2` |
| `formInstId` | String | 否 | 表单实例 ID |
| `procInstId` | String | 否 | 运行实例 ID |
| `startTime` | Number | 否 | 时间范围起点，毫秒时间戳 |
| `endTime` | Number | 否 | 时间范围终点，毫秒时间戳 |
| `pageIndex` | Number | 是 | 页码，从 1 开始 |
| `pageSize` | Number | 是 | 每页条数 |
