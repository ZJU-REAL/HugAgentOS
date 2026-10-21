
# 宜搭流程规则配置技能

## 严格禁止 (NEVER DO)

- 不要在流程定义中使用猜测的 fieldId，必须先用 `yida-get-schema` 获取
- 不要在未加载本技能内容的情况下编写流程定义 JSON，格式复杂且易出错
- 不要用此技能创建流程表单，应使用 `yida-create-process`
- 不要用 shell heredoc、`cat`/`echo`/`printf`/`tee` 或重定向生成流程定义 JSON

## 严格要求 (MUST DO)

- **发布前必须确认**：执行流程发布操作前，必须向用户展示流程配置摘要（节点数、审批人、条件分支），获得用户明确同意后再发布
- 字段 ≥ 3 且审批节点 ≥ 2 时，必须为每个节点配置字段权限
- 存在回退/循环语义时，必须配置 `routeRules` 跳转规则
- 配置前先用 `yida-get-schema` 获取所有字段 ID
- 流程定义 JSON 必须用结构化文件写入工具创建到 `<projectRoot>/.cache/openyida/<项目名或任务名>/`，不要在仓库根目录、系统临时目录或 `.cache/` 顶层生成 `process-definition.json` 等临时文件

## 适用场景

| 用户意图 | 触发条件 |
|---------|---------|
| 配置/修改审批流程 | "配置审批"、"审批节点"、"条件分支"、"字段权限" |
| 从零创建流程表单 | → 改用 `yida-create-process` |

## 触发条件

**正向触发**：
- "配置审批流程"、"设置审批节点"、"配置条件分支"
- "设置字段权限"、"配置抄送节点"、"设置跳转规则"
- 已有流程表单（processCode 已知），需要修改或配置审批规则
- `yida-create-process` 完成后，需要进一步配置复杂流程规则

---


## 概述

本技能描述如何通过流程设计器 API 为宜搭流程表单配置审批流程。支持审批节点、办理节点、条件分支、并行分支、嵌套分支、字段权限、抄送节点、跳转规则；同时支持宜搭 `yida-simple-flow` 官方组件节点的配置透传和 `processJson` 适配，用于连接器、数据、消息、邮件、Groovy/JavaScript、子流程、卡片、循环、AI 等高级节点。

## 何时使用

当以下场景发生时使用此技能：
- 用户需要为已有的流程表单配置审批流程
- 用户需要修改已有流程的审批规则
- 用户需要配置条件分支、嵌套分支等复杂流程
- 已通过 `yida-create-form-page` 创建表单后，需要配置流程规则

## 与 yida-create-process 的区别

| 场景 | 使用技能 |
|------|---------|
| **已有流程表单**：只需修改/配置已有表单的审批流程规则 | **本技能（yida-process-rule）** |
| **从零创建**：需要新建表单 + 配置审批流程（一步到位） | **yida-create-process** |

> 简单判断：有没有现成的流程表单？有 → 本技能；没有 → `yida-create-process`。

## 使用方式

```bash
openyida configure-process <appType> <formUuid> <processDefinitionFile> [processCode]
```

**参数说明**：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `appType` | 是 | 应用 ID，如 `APP_XXX` |
| `formUuid` | 是 | 表单 UUID，如 `FORM-XXX` |
| `processDefinitionFile` | 是 | 流程定义 JSON 文件路径 |
| `processCode` | 否 | 流程 Code，如 `TPROC--XXX`。不传则自动获取 |

**示例**：

```bash
openyida configure-process "APP_XXX" "FORM-YYY" .cache/openyida/order/process-definition.json
```

> 上方路径默认从 OpenYida project 工作目录执行；从 workspace 根执行命令时传 `project/.cache/openyida/order/process-definition.json`。

**输出**：日志输出到 stderr，JSON 结果输出到 stdout：

```json
{
  "success": true,
  "processCode": "TPROC--XXX",
  "processId": "83145794990",
  "processVersion": 2,
  "appType": "APP_XXX",
  "formUuid": "FORM-YYY"
}
```

## 流程定义 JSON 格式

流程定义文件描述审批流程的节点结构，脚本会自动转换为宜搭平台需要的 `processJson` 和 `viewJson`。

### 节点类型

| 类型 | 说明 | 必填属性 |
| --- | --- | --- |
| `approval` | 审批节点 | `name`, `approver` |
| `operator` | 办理 / 填写节点，审批人 DSL 相同，可用 `executor` 代替 `approver` | `name`, `executor` 或 `approver` |
| `route` | 条件分支路由 | `conditions` |
| `parallel` | 并行分支，满足条件的分支都会执行 | `branches` |
| `carbon` | 抄送节点 | `name`, `approver` |
| 官方组件节点 | 连接器、数据、消息、邮件、代码、子流程、卡片、循环、AI 等 | `type` 或 `componentName`，以及对应真实 props |

### 节点别名

OpenYida 会自动兼容常见别名：

| 写法 | 归一化类型 |
| --- | --- |
| `approval`, `ApprovalNode` | `approval` |
| `operator`, `executor`, `OperatorNode` | `operator` |
| `multiApproval`, `MultiApprovalNode` | `approval` 类型的多人审批组件 |
| `carbon`, `cc`, `CarbonNode` | `carbon` |
| `route`, `branch`, `ConditionContainer` | `route` |
| `parallel`, `parallelBranch` | `parallel` |

### 审批节点属性

| 属性 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `type` | String | 是 | 固定 `"approval"` |
| `name` | String | 是 | 节点名称 |
| `approver` | String/Object | 是 | 审批人。`"originator"` 表示发起人；Object 支持 `user`、`role`、`deptLeader`、`directLeader`，也可传入宜搭流程设计器的原始审批人配置 |
| `description` | String | 否 | 节点描述 |
| `formConfig` | Object | 否 | 字段权限配置 |
| `routeRules` | Array | 否 | 跳转规则 |

### 办理 / 填写节点（operator）

办理节点底层仍是宜搭流程的 `approval` 类型，但 `viewJson` 使用 `OperatorNode`，适合“资料补充”“填写处理结果”“业务办理”等非审批动作。

```json
{
  "type": "operator",
  "name": "资料补充",
  "executor": {
    "type": "user",
    "users": [
      { "id": "operator001", "name": "运营同学" }
    ]
  },
  "formConfig": {
    "behaviorList": [
      { "fieldId": "textareaField_material", "fieldBehavior": "NORMAL" },
      { "fieldId": "numberField_amount", "fieldBehavior": "READONLY" }
    ]
  }
}
```

### 条件分支属性

| 属性 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `type` | String | 是 | 固定 `"route"` |
| `conditions` | Array | 是 | 条件列表 |

### 并行分支属性

```json
{
  "type": "parallel",
  "name": "并行会审",
  "branches": [
    {
      "name": "法务会审",
      "childNodes": [
        { "type": "approval", "name": "法务审批", "approver": "originator" }
      ]
    },
    {
      "name": "财务会审",
      "rules": [
        {
          "fieldId": "numberField_amount",
          "fieldName": "金额",
          "componentType": "NumberField",
          "op": "GreaterThan",
          "value": 10000
        }
      ],
      "childNodes": [
        { "type": "approval", "name": "财务审批", "approver": "originator" }
      ]
    }
  ]
}
```

| 属性 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `branches` | Array | 是 | 并行分支列表，也兼容 `conditions` / `parallelBranches` |
| `branches[].name` | String | 否 | 分支名称 |
| `branches[].rules` | Array | 否 | 分支条件；不配置时表示所有数据进入该分支 |
| `branches[].logic` | String | 否 | `"AND"`（默认）或 `"OR"` |
| `branches[].childNodes` | Array | 否 | 分支内节点 |

### 条件定义

| 属性 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `name` | String | 是 | 条件名称 |
| `rules` | Array | 是 | 条件规则列表 |
| `logic` | String | 否 | 规则逻辑，`"AND"`（默认）或 `"OR"` |
| `childNodes` | Array | 否 | 条件满足时执行的子节点列表 |

### 条件规则

| 属性 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `fieldId` | String | 是 | 字段 ID，如 `selectField_xxx` |
| `fieldName` | String | 是 | 字段名称 |
| `op` | String | 是 | 操作符 |
| `value` | String/Array | 是 | 比较值 |
| `componentType` | String | 是 | 字段组件类型 |

### 支持的操作符

| 操作符 | 说明 | 适用类型 |
| --- | --- | --- |
| `Equal` | 等于 | 所有类型 |
| `NotEqual` | 不等于 | 所有类型 |
| `Contains` | 包含 | TextField |
| `NotContain` | 不包含 | TextField |
| `IsEmpty` | 为空 | 所有类型 |
| `IsNotEmpty` | 不为空 | 所有类型 |
| `GreaterThan` | 大于 | NumberField |
| `GreaterThanOrEqual` | 大于等于 | NumberField |
| `LessThan` | 小于 | NumberField |
| `LessThanOrEqual` | 小于等于 | NumberField |
| `In` | 属于 | SelectField, RadioField |
| `NotIn` | 不属于 | SelectField, RadioField |

### 字段权限配置（formConfig）

```json
{
  "formConfig": {
    "behaviorList": [
      { "fieldId": "textField_xxx", "fieldBehavior": "READONLY" },
      { "fieldId": "radioField_xxx", "fieldBehavior": "NORMAL" }
    ]
  }
}
```

| fieldBehavior | 说明 |
| --- | --- |
| `NORMAL` | 可编辑 |
| `READONLY` | 只读 |
| `HIDDEN` | 隐藏 |

### 跳转规则（routeRules）

```json
{
  "routeRules": [
    { "when": "disagree", "jumpTo": "部门主管审核" }
  ]
}
```

`jumpTo` 的值为目标审批节点的 `name`，或 `"结束"` 表示跳到流程结束。

### 审批人配置

#### 发起人本人

```json
{
  "type": "approval",
  "name": "发起人确认",
  "approver": "originator"
}
```

#### 指定成员

`id` 使用宜搭/钉钉通讯录中的 userId。可通过组织成员查询、应用成员配置或已知人员表获取。

```json
{
  "type": "approval",
  "name": "主管审批",
  "approver": {
    "type": "user",
    "users": [
      { "id": "manager7350", "name": "九神" }
    ],
    "multiApproverType": "all"
  }
}
```

#### 指定角色

`roleType` 默认为 `"YIDA"`；钉钉角色可传 `"DINGTALK"`。多个角色会按 `roleType` 生成宜搭流程需要的 `multiRoles`。

```json
{
  "type": "approval",
  "name": "财务审批",
  "approver": {
    "type": "role",
    "roles": [
      { "id": "ROLE-FINANCE", "name": "财务", "roleType": "YIDA" }
    ],
    "multiApproverType": "or"
  }
}
```

#### 部门主管 / 直属主管

```json
{
  "type": "approval",
  "name": "部门主管审批",
  "approver": {
    "type": "deptLeader",
    "source": "originator",
    "level": 1,
    "needLeaderReplace": true,
    "ignoreNoLeaderDept": false
  }
}
```

直属主管使用 `"type": "directLeader"`，其余参数相同。`multiApproverType` 可选 `"all"`、`"or"`、`"oneByOne"`。

### 原始审批人配置（高级）

当 DSL 不能覆盖某个租户/版本的特殊审批规则时，可在节点上提供原始审批配置，OpenYida 会透传到 `processJson` 和 `viewJson`：

```json
{
  "type": "approval",
  "name": "部门负责人审批",
  "approver": {
    "approvalType": "ext_target_approval_originator",
    "approvals": [["originator"]],
    "approverRules": {
      "type": "ext_target_approval_originator",
      "mode": "ApprovalNode_rules_only",
      "approverList": [{ "type": "ext_target_approval" }],
      "multiApproverType": "all",
      "conditionalMode": "conditional",
      "description": "发起人本人"
    }
  }
}
```

说明：不同租户/版本下某些高级规则（接口人、第三方服务、连接器、权限矩阵等）的 `approverRules` 结构可能不同。遇到 DSL 暂未覆盖的类型时，先在宜搭流程设计器中配置一个样例并导出/抓取对应结构，再用该对象固化到流程定义中。

## 官方组件节点（高级透传）

完整支持清单、连接器示例、数据 / 消息 / 代码节点示例和透传规则详见 [references/official-component-nodes.md](references/official-component-nodes.md)。

关键边界：

- 常规审批/办理/抄送、条件/并行分支优先用本文件中的简化 DSL。
- 连接器、数据、消息、邮件、Groovy/JavaScript、子流程、卡片、循环、AI 等节点使用真实组件 props 透传，并按线上设计器规则转换 `processJson.props`。
- 不认识的节点类型会直接报错，不会静默跳过，避免生成断链流程。
- 从零创建复杂集成自动化时优先使用 `yida-integration`；本技能用于流程表单规则里需要高级组件节点、或已有真实节点配置需要固化的场景。

## AI 自动生成流程特性（必须遵守）

> 📖 AI 生成流程定义的完整规范（字段权限自动生成、回退规则识别、检查清单）详见 [references/process-ai-rules.md](references/process-ai-rules.md)。

---

## 使用示例

### 示例 1：简单审批流程

```json
{
  "nodes": [
    {
      "type": "approval",
      "name": "主管审批",
      "approver": "originator"
    }
  ]
}
```

流程：`发起 → 主管审批 → 结束`

> 📖 更多示例（条件分支、嵌套分支、字段权限、跳转规则、自定义详情页等）详见 [references/examples.md](references/examples.md)。

## 注意事项

- 所有 `fieldId` 必须通过 `openyida get-schema` 获取，不能手写猜测
- 条件分支会自动补一个“其他情况”默认分支；不要手写猜测低层 `conditionNode` 结构
- 嵌套分支不超过 3 层
- 流程发布前必须先保存
- **本技能不读写 memory**：流程定义通过 CLI 命令写入宜搭平台，processCode 等信息输出到 stdout，不依赖跨会话的 memory 状态

## 异常处理

| 异常场景 | 处理方式 |
|---------|----------|
| 流程保存失败 | 检查 processCode 是否正确，确认登录态有效 |
| 条件分支报错 | 确认最后一个 conditionNode 的 `conditionType` 为 `"else"` |
| fieldId 不存在 | 先执行 `openyida get-schema` 获取真实 fieldId，不能手写猜测 |
| 流程发布后未生效 | 确认已执行发布步骤（save 后还需 publish），检查流程版本 |
| 嵌套分支超过 3 层 | 重新设计流程结构，将复杂条件拆分为多个节点 |
## 前置依赖

- Node.js ≥ 16
- 项目根目录存在 `.cache/cookies.json`（首次运行会自动触发扫码登录）

## 文件结构

```
yida-process-rule/
└── SKILL.md                    # 本文档
```

## 与其他技能配合

| 步骤 | 技能 | 说明 |
| --- | --- | --- |
| 1 | `yida-create-app` | 创建应用，获取 `appType` |
| 2 | `yida-create-form-page` | 创建表单，获取 `formUuid` 和字段 ID |
| 3 | **本技能** | 配置表单的流程规则 |
| 4 | `yida-custom-page` | 编写自定义页面代码 |
| 5 | `yida-publish-page` | 发布自定义页面 |

> **快捷方式**：使用 `yida-create-process` 技能可一键完成步骤 2-3（创建表单 + 配置流程）。
