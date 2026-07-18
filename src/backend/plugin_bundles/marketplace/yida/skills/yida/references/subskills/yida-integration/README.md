
# yida-integration — 宜搭集成&自动化（逻辑流）技能

## 严格禁止 (NEVER DO)

- 不要在未加载本技能内容的情况下编写逻辑流定义，节点格式复杂且易出错
- 不要编造 formUuid 或 fieldId，必须从已有记录或 `yida-get-schema` 获取
- 不要用此技能配置审批流程，应使用 `yida-process-rule`

## 严格要求 (MUST DO)

- **创建/发布前必须确认**：执行集成自动化创建或发布操作前，必须向用户展示逻辑流配置摘要（触发条件、节点列表、通知对象），获得用户明确同意后再执行
- 创建前先确认触发表单的 formUuid 和相关字段 ID
- 创建成功后记录逻辑流 ID 到 `.cache/<项目名>-schema.json`
- `--spec`、连接器输入等 JSON 文件必须先用结构化文件写入工具创建到 `<projectRoot>/.cache/openyida/<项目名或任务名>/integration/`；不要用 shell heredoc、`cat`/`echo`/`printf`/`tee` 或重定向写文件，也不要写仓库根目录或系统临时目录
- 参考官方示例时不要只看默认页面 schema：集成自动化示例的默认页通常只是触发表单或说明页，逻辑流本体需要通过集成自动化接口/命令查询或创建

## 适用场景

| 用户意图 | 触发条件 |
|---------|---------|
| 表单提交后自动通知 | "自动通知"、"数据变更触发"、"集成&自动化" |
| 数据操作自动化 | "自动新增"、"自动更新"、"逻辑流" |
| 人工审批流程 | → 改用 `yida-process-rule` |

## 异常处理

| 异常场景 | 处理方式 |
|---------|----------|
| formUuid 不存在 | 不得编造，必须从已有记录或 `yida-get-schema` 获取 |
| 逻辑流创建失败 | 检查节点配置格式，确认触发表单和目标表单存在 |
| 通知接收人为空 | 必须指定至少一个 userId，不得留空 |
| 变量引用格式错误 | 确认使用 `#{fieldId-ComponentType}#` 格式 |
| 发布失败 | 检查逻辑流配置完整性，确认登录态有效 |

## Agent 错误处理策略

当 Agent 执行本技能遇到错误时，必须遵循以下默认行为：

| 错误类型 | 默认处理策略 |
|---------|-------------|
| 命令执行失败 | 停止执行，向用户展示错误信息，询问是否重试或调整参数 |
| 参数缺失（appType/formUuid/userId 等） | 主动询问用户补充，不得猜测或编造 |
| 权限不足 / 登录态失效 | 停止执行，提示用户执行 `openyida auth status` 检查登录态 |
| 节点配置格式错误 | 停止执行，展示错误详情，引导用户参照文档修正配置 |
| 网络超时 | 重试 1 次，仍失败则停止并提示用户检查网络 |
| 未知错误 | 停止执行，完整展示错误信息，建议用户反馈问题 |

---


本技能用于在宜搭平台创建「集成&自动化」（逻辑流），支持场景：**表单事件触发 → 多节点组合处理 → 钉钉工作通知 / 数据操作**。

官方示例中心体现的集成范式是“表单收集数据，逻辑流处理副作用”。因此，跨表新增/更新、通知、创建待办、调用钉钉能力等提交后动作，默认用本技能；不要把这些副作用塞进表单字段 JS 或自定义页面按钮，除非用户明确要求一次性人工触发工具页。

## 功能概述

- 监听指定表单的新增 / 更新 / 删除 / 评论事件
- 可选：从另一张表单获取单条数据，支持按触发表单字段值过滤
- 可选：向指定表单新增数据，支持字段赋值（含公式）
- 可选：更新指定表单数据（按节点查询结果或条件过滤）
- 可选：条件分支，根据上游节点数据有无 / 字段值走不同分支
- 通知内容和标题支持引用表单字段变量（`#{fieldId-ComponentType}#` 格式）
- 支持保存为草稿（未开启状态）或直接发布（开启状态）

## 命令格式

```bash
openyida integration create <appType> <formUuid> <flowName> [选项]
openyida integration check <appType...> [--json] [--output result.xlsx] [--no-progress]
```

### 参数说明

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `appType` | 是 | 应用 ID，如 `APP_XXXX` |
| `formUuid` | 是 | 触发表单 UUID，如 `FORM-XXXX` |
| `flowName` | 是 | 逻辑流名称 |

### 选项说明

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `--process-code <code>` | 自动生成 | 已有逻辑流的 processCode（`LPROC-xxx` 格式），不传则自动生成 |
| `--receivers <userId,...>` | 空（无接收人） | 接收钉钉工作通知的用户 ID，多个用逗号分隔 |
| `--title <title>` | 同 flowName | 通知标题，支持 `#{fieldId-ComponentType}#` 引用表单字段 |
| `--content <content>` | `"表单有新记录提交，请及时查看。"` | 通知内容，支持 `#{fieldId-ComponentType}#` 引用表单字段 |
| `--events <insert,update>` | `insert` | 触发事件，可选值：`insert`/`update`/`delete`/`comment`/`processFinish`/`activityTask`（也支持别名 `create`/`approval`/`approvalNode`），多个用逗号分隔 |
| `--approval-actions <agree,...>` | 空 | 当 `--events processFinish` 或 `--events activityTask` 时必填；可选值：`agree`/`disagree`/`terminated`，多个用逗号分隔 |
| `--approval-node-ids <nodeId,...>` | 空 | 当 `--events activityTask` 时必填；审批节点 ID，多个用逗号分隔 |
| `--trigger-condition <fieldId:fieldName:opCode:value[:componentType[:valueType]]>` | 空 | 触发器过滤条件，可多次传入；示例：`radioField_xxx:采购类型:Equal:材料采购:RadioField:literal` |
| `--trigger-recursively` | 关闭 | 允许自动触发，对应设计器里的“允许自动触发” |
| `--spec <file.json>` | 不启用 | 使用结构化编排文件创建复杂自动化，支持 `getSelf`、`dataRetrieve`、`dataCreate`、`dataUpdate`、`route`、`sendMessage`、`connector` |
| `--get-self` | 关闭 | 自动插入“获取自身”节点：来源表单为当前触发表，过滤条件为 `pid 等于 字段 __masterdata_form_inst_id` |
| `--get-self-field <field>` | `__masterdata_form_inst_id` | 覆盖右侧触发事件系统字段；仅在确认环境变量名不同后使用 |
| `--get-self-query-field <field>` | `pid` | 覆盖左侧查询系统字段；仅在确认平台查询字段名不同后使用 |
| `--data-form-uuid <formUuid>` | 不启用 | 获取单条数据节点的目标表单 UUID（B 表单），传入后在触发节点和通知节点之间插入 GetSingleDataNode |
| `--data-condition <bFieldId:bFieldName:aFieldId[:componentType[:opCode[:valueType]]]>` | 无 | 获取单条数据的过滤条件，可多次传入；格式：`B表单字段ID:B表单字段名:A表单字段ID[:组件类型[:操作符[:值类型]]]`，组件类型默认 `TextField`，操作符默认 `Contain` |
| `--add-data-form-uuid <formUuid>` | 不启用 | 新增数据节点的目标表单 UUID，传入后在通知节点之后插入 AddDataNode；目标必须是普通表单（如 `formType=receipt`），不能是流程表单（`formType=process`） |
| `--add-data-assignment <targetFieldId:valueType:value>` | 无 | 新增数据的字段赋值，可多次传入；格式：`目标字段ID:valueType:value`，valueType 可选 `processVar`（引用触发表单字段）/ `literal`（固定值）/ `column`（公式） |
| `--initiate-approval-form-uuid <formUuid>` | 不启用 | 发起审批节点的目标流程表单 UUID；当 B 是流程表单（`formType=process`）时必须使用它，不要用 `--add-data-form-uuid` |
| `--initiate-approval-initiator-user <userId[:name]>` | 无 | 发起审批的发起人，推荐格式 `01376266634908:张三`；只传 userId 时设计器中会显示原始 ID。使用发起审批节点时必填 |
| `--initiate-approval-assignment <targetFieldId:valueType:value>` | 无 | 发起审批时写入目标流程表单字段的赋值规则，可多次传入；格式同 `--add-data-assignment` |
| `--connector-mode <mode>` | 自动推断 | 连接器类型；HTTP 自定义连接器使用 `5`，`connectorId` 以 `Http_` 开头时会自动按 `5` 处理 |
| `--connection-id <id>` | 空 | HTTP 连接器鉴权连接 ID；HTTP 连接器建议传入，否则设计器右侧配置面板可能无法加载连接实例详情 |
| `--connector-display-name <name>` | `--connector-name` | 连接器展示名称，用于设计器画布和右侧配置面板 |
| `--publish` | 不发布 | 加此标志则保存后立即发布（开启状态），否则仅保存为草稿 |

### 示例

```bash
# 最简用法：表单新增时通知指定用户，仅保存草稿
openyida integration create APP_XXX FORM-XXX "新增记录通知" \
  --receivers user123 \
  --title "有新记录提交" \
  --content "表单有新记录提交，请及时处理。"

# 引用表单字段变量，保存并发布
openyida integration create APP_XXX FORM-XXX "记录变更通知" \
  --receivers user123,user456 \
  --title "记录变更：#{textField_abc-TextField}#" \
  --content "内容：#{textField_abc-TextField}#" \
  --events insert,update,delete,comment \
  --publish

# 带获取单条数据节点：触发时从 B 表单获取匹配记录，再发送通知
# --data-condition 格式：B表单字段ID:B表单字段名:A表单字段ID[:组件类型[:opCode[:valueType]]]
# 可多次传入 --data-condition 添加多个过滤条件
openyida integration create APP_XXX FORM-A-XXX "跨表通知" \
  --receivers user123 \
  --title "关联记录变更：#{textField_a1-TextField}#" \
  --content "B表单数据已更新，请查看。" \
  --events insert,update \
  --data-form-uuid FORM-B-XXX \
  --data-condition "textField_b1:B表单姓名字段:textField_a1:TextField" \
  --publish

# 获取自身：触发后重新读取当前记录，避免流水号、定时值或触发 payload 不是最新值
openyida integration create APP_XXX FORM-A-XXX "获取自身后通知" \
  --title "记录已提交" \
  --content "已按表单实例ID重新读取当前记录。" \
  --events insert,update \
  --get-self \
  --publish

# 带新增数据节点：触发时将 A 表单数据同步到 B 表单，并发送通知
# --add-data-assignment 格式：目标字段ID:valueType:value
# valueType 可选：processVar（引用触发表单字段）、literal（固定值）、column（公式）
# 可多次传入 --add-data-assignment 添加多个字段赋值
openyida integration create APP_XXX FORM-A-XXX "表单A新增后同步到表单B并通知" \
  --receivers user123 \
  --title "数据已同步" \
  --content "表单A新增了一条记录，已自动同步到表单B。" \
  --add-data-form-uuid FORM-B-XXX \
  --add-data-assignment "textField_b1:processVar:textField_a1" \
  --add-data-assignment "numberField_b2:literal:0" \
  --add-data-assignment "textareaField_b3:column:CONCATENATE(#{textField_a1},#{textField_a2})" \
  --publish

# A 流程审批完成后，发起 B 流程审批（B 是流程表单时使用）
openyida integration create APP_XXX FORM-A-XXX "A审批完成后发起B流程" \
  --events processFinish \
  --approval-actions agree \
  --get-self \
  --initiate-approval-form-uuid FORM-PROCESS-B-XXX \
  --initiate-approval-initiator-user "01376266634908:张三" \
  --initiate-approval-assignment "textField_b1:processVar:textField_a1" \
  --initiate-approval-assignment "textareaField_b2:literal:自动发起" \
  --publish

# 调用 HTTP 自定义连接器动作，并保留设计器右侧面板所需的连接器元信息
openyida integration create APP_XXX FORM-A-XXX "调用 HTTP 连接器" \
  --connector-id Http_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
  --action-id publish_month_qs \
  --connection-id 28336 \
  --connector-display-name "BI 后端" \
  --connector-assignment "month:processVar:textField_month" \
  --publish
```

### 结构化编排 `--spec`

复杂自动化优先使用 `--spec`，不要手写 `saveProcess` payload。spec 的节点可以用 `id` 作为别名，后续用 `${别名}.fieldId` 引用上游节点输出，OpenYida 会在保存前替换成真实 `node_xxx`。

```json
{
  "events": ["insert"],
  "nodes": [
    { "id": "self", "type": "getSelf" },
    {
      "id": "branch",
      "type": "route",
      "branches": [
        {
          "id": "hasSelf",
          "name": "已获取自身",
          "conditions": [
            {
              "fieldId": "${self}.pid",
              "fieldName": "表单实例ID",
              "opCode": "ExistValue",
              "componentType": "TextField"
            }
          ],
          "nodes": [
            {
              "id": "updateSelf",
              "type": "dataUpdate",
              "source": "self",
              "assignments": [
                {
                  "column": "textareaField_result",
                  "valueType": "literal",
                  "value": "已处理"
                }
              ]
            }
          ]
        },
        {
          "id": "fallback",
          "name": "其他情况",
          "default": true,
          "nodes": [
            {
              "id": "notice",
              "type": "sendMessage",
              "title": "未获取到记录",
              "content": "获取自身节点无匹配结果，请检查过滤条件。"
            }
          ]
        }
      ]
    }
  ]
}
```

```bash
openyida integration create APP_XXX FORM-XXX "获取自身后分支更新" \
  --spec .cache/openyida/<项目名或任务名>/integration/get-self-update.json \
  --publish
```

> `--spec` 文件先用 create_file / Write / file edit tool 创建。上方路径默认从 OpenYida project 工作目录执行；从 workspace 根执行命令时路径加 `project/` 前缀。

## 字段变量引用格式

在通知标题和内容中，可以使用 `#{fieldId-ComponentType}#` 格式引用触发表单的字段值：

```
#{textField_mmq4ldti-TextField}#
#{numberField_abc123-NumberField}#
#{selectField_xyz-SelectField}#
```

- `fieldId`：字段 ID（可通过 `yida-get-schema` 技能查询）
- `ComponentType`：字段组件类型（如 `TextField`、`NumberField`、`SelectField` 等）

## 输出结果

命令执行成功后，向 stdout 输出 JSON：

```json
{
  "success": true,
  "published": false,
  "processCode": "LPROC-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
  "flowName": "新增记录通知",
  "appType": "APP_XXX",
  "formUuid": "FORM-XXX",
  "formEventTypes": ["insert"]
}
```

加 `--publish` 后 `published` 为 `true`。若发布失败，`published` 为 `false` 并附带 `warning` 字段说明原因。

## 异常日志检查

```bash
openyida integration check APP_XXX --json
openyida integration check APP_XXX APP_YYY --output project/output/自动化异常.xlsx
openyida integration diagnose --text "连接器异常：接口参数异常"
openyida integration diagnose --file project/tickets/automation-error.txt --json
```

- 会分页查询指定应用下的全部集成自动化，默认覆盖 `1/2/3/5/6` 五类触发类型。
- 对每条自动化调用运行日志接口，并按 `status=2` 筛选“执行异常”。
- `integration check` 会在 JSON/Excel/文本输出中附带诊断建议；注意“未发现异常日志”不等于业务一定正确，获取数据无匹配或条件未命中可能仍显示成功。
- `integration diagnose` 可离线诊断工单文本、OCR 后的截图文本或日志片段，不需要登录态。
- 批量查询时只显示单行进度，不逐条输出 HTTP 200。
- JSON 结果包含 `totalFlows`、`abnormalFlows[].processCode`、自动化名称、触发表单和异常日志列表。
- 传入 `--output <file.xlsx>` 时导出 Excel，一个应用一个 sheet；无异常的应用会写入“未发现执行异常日志”，检查失败的应用会写入失败原因。

## 集成自动化闭坑规则

- 获取自身：优先使用 `--get-self`，标准条件为查询侧系统字段 `pid` 等于触发事件字段 `__masterdata_form_inst_id`。不要用 `formInstId = formInstId`、不要用“包含”或非唯一字段做自身匹配。
- 流水号：新增/编辑触发时，触发 payload 中的流水号可能为空或不是最新值；需要先获取自身，再引用获取节点里的流水号。
- 定时自动化：定时触发值可能是历史数据；需要最新值时先获取自身或获取目标数据。
- 直接更新：匹配字段只能消费当前触发表字段，不能随意匹配前置节点字段；文本字段不要匹配单选/多选字段。直接更新不会触发被更新表单上的集成自动化。
- 条件分支：空值判断优先用公式 `ISEMPTY()`，不要只依赖“没有值”选项。
- 异常重试：重试通常会从头执行，已执行过的新增/更新节点可能再次写入或覆盖数据；重试前先确认幂等性。

## 调用流程

1. 读取 `.cache/cookies.json` 获取登录态（不存在则触发扫码登录）
2. 若未传入 `--process-code`，调用 `createLogicflow.json` 接口新建绑定关系，获取真实 `processCode`
3. 生成各节点 ID（`node_xxx` 格式，随机生成）
4. 根据用户传入的节点配置，构建 `json` 参数（节点定义）和 `viewJson` 参数（画布 Schema）
5. 调用 `saveProcess` 接口（`isOnline=false`）保存为草稿
6. 若指定 `--publish`，再次调用 `saveProcess` 接口（`isOnline=true`）发布生效

> ⚠️ **必须先调用 `createLogicflow.json` 新建绑定关系**，再调用 `saveProcess` 写入内容。直接调用 `saveProcess` 无法创建新逻辑流，只能覆盖更新已有逻辑流。

## 逻辑流节点结构

复杂节点 JSON 不再内联在主技能中，避免每次触发都占用大量上下文。需要手写或排查节点结构时，只读取对应参考文档：

- 节点类型速查、trigger / sendMessage / dataCreate / dataRetrieve / dataUpdate / route + condition 完整结构： [references/integration-node-schemas.md](references/integration-node-schemas.md)
- 常见创建示例： [references/examples.md](references/examples.md)

快速链路：

```text
trigger -> sendMessage -> finish
trigger -> dataRetrieve -> sendMessage -> finish
trigger -> sendMessage -> dataCreate -> finish
trigger -> dataRetrieve -> route -> condition -> dataUpdate/dataCreate -> finish
```

读取规则：

- 仅使用 CLI 选项创建标准通知、查询、新增数据链路时，不需要展开节点 JSON 参考。
- 需要自定义复杂分支、更新数据、排查 saveProcess payload 时，再读取 `integration-node-schemas.md`。
- 需要给用户展示示例命令时，优先读取 `examples.md`。

## 变量引用与字段赋值

> 📖 字段赋值 valueType 规律和变量引用格式对照详见 [references/integration-node-schemas.md](references/integration-node-schemas.md)。

## 接口说明

> 📖 saveProcess、listLogicflows、switchLogicflow 接口的完整参数和返回值结构详见 [references/integration-node-schemas.md](references/integration-node-schemas.md)。

## 前置依赖

- Node.js
- 项目根目录存在 `.cache/cookies.json`（首次运行会自动触发扫码登录）

## 文件结构

```
lib/
└── integration/
    └── integration-create.js    # integration create 子命令实现
```

## 与其他技能配合

1. **创建应用** → 使用 `yida-create-app` 技能获取 `appType`
2. **创建表单页面** → 使用 `yida-create-form-page` 技能获取 `formUuid`
3. **查询字段 ID** → 使用 `yida-get-schema` 技能获取 `fieldId` 和 `ComponentType`，用于构建字段变量引用
4. **创建集成&自动化** → 本技能，传入 `appType` 和 `formUuid`
5. **公式计算** → 当需要在赋值中使用复杂公式（如 `CONCATENATE`、`IF`、`SUM` 等）时，参考 `yida-formula` 技能（独立技能，待完善）；目前已支持基础公式：
   - 字符串拼接：`CONCATENATE(#{fieldId_a},#{fieldId_b})`
   - 数值运算：`${nodeId}.numberField_xxx+1`

## 触发条件

**正向触发**：
- "配置集成自动化"、"数据联动"、"自动触发"
- "表单提交后自动发消息"、"自动新增数据"
- "配置逻辑流"、"自动化规则"

## 注意事项

- `--receivers` 填写的是宜搭/钉钉用户 ID（`userId`），不是姓名
- 触发事件使用 API 内部名称：`insert`（新增）、`update`（更新）、`delete`（删除）、`comment`（评论），也支持别名 `create`
- `processCode` 格式为 `LPROC-` 加 38 位大写字母数字，不传则自动随机生成
- 保存（草稿）和发布使用**同一个接口** `saveProcess`，通过 `isOnline` 参数区分
- 错误码处理：接口返回 `errorCode: "TIANSHU_000030"`（csrf 校验失败）时，脚本会自动刷新 token 后重试；`errorCode: "307"`（登录过期）时，会自动重新登录后重试
- **本技能不读写 memory**：集成逻辑流配置通过 CLI 命令写入宜搭平台，processCode 等信息输出到 stdout，不依赖跨会话的 memory 状态
