
# 表单页面创建与更新

## 严格禁止 (NEVER DO)

- 不要编造 formUuid，必须从命令返回的 JSON 中提取
- 不要在 update 模式中使用猜测的 fieldId，必须先用 `yida-get-schema` 获取
- 不要用此命令操作数据记录（增删改查），应使用 `yida-data-management`
- 不要用 shell heredoc、`cat`/`echo`/`printf`/`tee` 或重定向生成字段、变更、补丁、规则、数据源 JSON 文件

## 严格要求 (MUST DO)

- create 成功后，将 formUuid 记录到 `.cache/<项目名>-schema.json`
- update 模式修改字段前，必须先用 `openyida get-schema` 确认字段 ID
- 字段定义或变更定义需要落盘时，必须使用 agent 的结构化文件写入工具创建到 `<projectRoot>/.cache/openyida/<项目名或任务名>/`，例如 `<projectRoot>/.cache/openyida/pm/pm-fields-team.json`；不要在仓库根目录、系统临时目录或 `.cache/` 顶层生成 `*-fields*.json`、`*-changes*.json`
- **本技能不读写 memory**：formUuid 等信息输出到 stdout，通过 `.cache/<项目名>-schema.json` 持久化，不依赖跨会话的 memory 状态

## 官方表单示例范式

官方示例中心的表单类能力大多用 `FormContainer + 标准字段 + 字段属性/公式/联动` 承载，少量 `RichText` 用于说明。创建或更新表单时优先按这个顺序落地：

1. 字段结构：用 `TextField`、`NumberField`、`DateField`、`EmployeeField`、`SelectField`、`TableField`、`AssociationFormField` 等标准字段表达数据模型。
2. 字段公式：计算、默认值、日期/文本转换等用字段 `valueType: "formula"`、`complexValue.formula`、`formula`，不要改写成自定义页面 JS。
3. 字段联动：显示隐藏、只读、onChange 自动赋值优先用 `rule` 模式；只有 OpenYida DSL 不覆盖的平台属性才用 `patch`。
4. 说明/示例文字：需要解释能力时可增加 `RichText` 或说明字段，但业务字段仍应保持结构化。
5. 提交后跨表/通知/流程动作不要塞进字段 JS；分别交给 `yida-integration`、`yida-process-rule` 或 `yida-connector`。

## 适用场景

用户需要"创建表单"、"新增字段"、"修改表单结构"时使用。

**关键区分**：
- 修改表单结构（字段增删改）→ 本技能 update 模式
- 操作表单中的数据记录 → `yida-data-management`

## 触发条件

**正向触发**：
- "创建表单"、"新建表单"、"添加表单"
- "新增字段"、"添加字段"、"修改字段"、"删除字段"
- "修改表单结构"、"更新表单"
- "给表单设计器 Schema 打补丁"、"配置 OpenYida 尚未封装的字段属性/动作"
- "字段显示/隐藏联动"、"字段值变化自动赋值"、"onChange 自动带出"
- "搜索选择字段绑定数据源"、"下拉字段远程搜索"、"选择字段从接口加载选项"
- 已有 appType，需要在应用下建立数据收集入口

---


## create 模式

```bash
openyida create-form create <appType> <formTitle> <fieldsJsonOrFile> [--layout double|card] [--theme compact|comfortable] [--label-align top|left]
# 文件路径示例：.cache/openyida/<项目名或任务名>/<表单名>-fields.json
```

> 文件先用 create_file / Write / file edit tool 创建。上方路径默认从 OpenYida project 工作目录执行；如果从 workspace 根执行命令，传 `project/.cache/openyida/<项目名或任务名>/<表单名>-fields.json`。

| 参数 | 必填 | 说明 |
|------|------|------|
| `appType` | 是 | 应用 ID |
| `formTitle` | 是 | 表单名称 |
| `fieldsJsonOrFile` | 是 | 字段定义 JSON 字符串（以 `[` 开头）或文件路径 |

### 输出

```json
{"success":true,"formUuid":"FORM-XXX","formTitle":"用户信息表","appType":"APP_xxx","fieldCount":4,"url":"{base_url}/APP_xxx/workbench/FORM-XXX"}
```

## update 模式

```bash
openyida create-form update <appType> <formUuid> <changesJsonOrFile>
# 文件路径示例：.cache/openyida/<项目名或任务名>/<表单名>-changes.json
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `appType` | 是 | 应用 ID |
| `formUuid` | 是 | 表单 UUID |
| `changesJsonOrFile` | 是 | 修改定义 JSON 字符串或文件路径 |

### 输出

```json
{"success":true,"formUuid":"FORM-YYY","appType":"APP_XXX","changesApplied":3,"url":"{base_url}/APP_XXX/workbench/FORM-YYY"}
```

## patch 模式（设计器 Schema 补丁）

当宜搭后台已有配置项，但 OpenYida 还没有高阶 DSL 时，使用 patch 模式对表单 V5 Schema 做受控修改：

```bash
openyida create-form patch <appType> <formUuid> <patchJsonOrFile>
# 文件路径示例：.cache/openyida/<项目名或任务名>/<表单名>-patch.json
```

支持的操作：

| 操作 | 说明 |
|------|------|
| `field-props` | 按 `fieldId` / `field` / `label` 合并字段 `props`，适合补字段状态、提交策略、设计器属性 |
| `form-props` | 合并 `FormContainer.props` |
| `add` / `replace` / `remove` | JSON Pointer 形式的底层 Schema Patch |
| `merge` | 对指定 JSON Pointer 路径做对象深合并 |
| `actions-module` | 写入页面动作模块 `source` / `compiled`，`source` 会自动编译 |
| `bind-field-action` | 给字段事件（如 `onChange`）绑定动作引用 |
| `bind-datasource` | 给选项类字段绑定远程搜索数据源（高阶入口优先用 bind-datasource 模式） |

示例：把字段设为隐藏但仍提交，用于后续公式或动作计算：

```json
[
  {
    "action": "field-props",
    "fieldId": "textField_xxx",
    "props": { "behavior": "HIDDEN", "submittable": "ALWAYS" }
  }
]
```

patch 模式是高级能力：执行前必须先 `openyida get-schema <appType> <formUuid> --json` 确认现有结构，补丁文件必须用结构化文件写入工具写在 `<projectRoot>/.cache/openyida/<项目名或任务名>/` 下。

### 自定义校验函数（customValidate）

通过 patch 模式的 `field-props` 给字段绑定自定义校验函数。支持两种写法：

**写法一：直接在组件自定义函数中写完整逻辑（推荐）**

不依赖 JS 面板，校验逻辑自包含在字段的 validation 中：

```json
[
  {
    "action": "field-props",
    "fieldId": "numberField_xxx",
    "props": {
      "validation": [
        {
          "type": "customValidate",
          "param": {
            "source": "function validateRule(value) {\n  var values = (this.item && this.item.values) || {};\n  var stock = Number(values.numberField_yyy) || 0;\n  return Number(value) <= stock;\n}",
            "type": "js",
            "error": {}
          },
          "message": { "zh_CN": "领用数量不能超过当前库存", "en_US": "Quantity cannot exceed current stock", "type": "i18n" }
        }
      ]
    }
  }
]
```

> `compiled` 字段可省略，系统会自动从 `source` 编译生成。

**写法二：桥接调用 JS 面板函数**

先通过 `actions-module` 将函数写入 JS 面板，再在字段自定义函数中通过 `this.xxx()` 调用：

```json
[
  {
    "action": "actions-module",
    "source": "export function validateOutboundQty(value) {\n  var values = (this.item && this.item.values) || {};\n  var stock = Number(values.numberField_yyy) || 0;\n  return Number(value) <= stock;\n}"
  },
  {
    "action": "field-props",
    "fieldId": "numberField_xxx",
    "props": {
      "validation": [
        {
          "type": "customValidate",
          "param": {
            "source": "function validateRule(value) {\n  return this.validateOutboundQty(value);\n}",
            "type": "js",
            "error": {}
          },
          "message": { "zh_CN": "领用数量不能超过当前库存", "en_US": "Quantity cannot exceed current stock", "type": "i18n" }
        }
      ]
    }
  }
]
```

**关键规则：**
- `param` 必须是 `{ source, type: "js", error: {} }` 格式，**不能用** `{ actionType, actionName }` 或 `{ type: "JSExpression" }` 格式
- `source` 中的函数名必须是 `validateRule`
- 函数返回 `true` 表示校验通过，`false` 表示校验失败
- 子表内字段通过 `this.item.values` 获取同行其他字段的值

## rule 模式（字段联动与自动赋值）

常见字段联动不用直接写底层 Schema Patch，使用 rule 模式：

```bash
openyida create-form rule <appType> <formUuid> <rulesJsonOrFile>
# 文件路径示例：.cache/openyida/<项目名或任务名>/<表单名>-rules.json
```

支持的规则类型：

| 类型 | 说明 |
|------|------|
| `visibility` / `show-hide` | 根据字段值动态设置目标字段 `NORMAL` / `HIDDEN` / `READONLY` |
| `set-value` / `assign` | 在字段 `onChange` 时给目标字段赋固定值、复制字段值、模板值或 JS 表达式结果 |

示例：部门为“技术部”时显示预算说明，否则隐藏：

```json
[
  {
    "type": "visibility",
    "when": { "field": "部门", "operator": "eq", "value": "技术部" },
    "target": "预算说明",
    "behavior": "NORMAL",
    "elseBehavior": "HIDDEN"
  }
]
```

示例：状态变化时自动带出处理说明：

```json
[
  {
    "type": "set-value",
    "on": "状态",
    "target": "处理说明",
    "template": "当前状态：{{状态}}"
  }
]
```

示例：用 JS 表达式计算目标值，表达式可使用 `value`（触发字段值）和 `fields`（按字段名/fieldId 映射的当前表单值）：

```json
[
  {
    "type": "set-value",
    "on": "数量",
    "target": "金额",
    "expression": "Number(value || 0) * Number(fields['单价'] || 0)"
  }
]
```

rule 模式会自动生成宜搭动作代码，绑定触发字段的 `onChange`，并在页面加载/表单数据初始化后执行一次规则。若字段已有 `onChange` 动作，OpenYida 会先调用原动作，再执行生成的规则。

## bind-datasource 模式（选项字段远程搜索数据源）

当 `SelectField` / `MultiSelectField` / `RadioField` / `CheckboxField` 不是固定选项，而是需要从接口、连接器代理或宜搭内部接口搜索加载时，使用 bind-datasource 模式：

```bash
openyida create-form bind-datasource <appType> <formUuid> <fieldLabelOrId> <dataSourceJsonOrFile>
# 文件路径示例：.cache/openyida/<项目名或任务名>/<字段名>-datasource.json
```

数据源配置示例：

```json
{
  "url": "/query/deptService/searchDepts.json",
  "dataType": "json",
  "queryParam": "key",
  "listPath": "values",
  "labelField": "name",
  "valueField": "deptId",
  "notFoundContent": "无匹配数据"
}
```

如果接口返回结构复杂，可以直接提供宜搭运行时 `beforeFetch` / `afterFetch` 函数源码：

```json
{
  "url": "https://example.com/api/customers",
  "dataType": "json",
  "beforeFetch": "function willFetch(params) { params.keyword = params.key || ''; return params; }",
  "afterFetch": "function didFetch(content) { return (content.data || []).map(function(item) { return { text: item.name, value: item.id }; }); }"
}
```

创建字段时也可直接在字段定义里写 `remoteDataSource`：

```json
[
  {
    "type": "SelectField",
    "label": "客户",
    "remoteDataSource": {
      "url": "https://example.com/api/customers",
      "listPath": "data",
      "labelField": "name",
      "valueField": "id"
    }
  }
]
```

说明：bind-datasource 负责写入字段的 `searchConfig`、`defaultDataSource.searchConfig`、`filterLocal=false` 和初始选项；如果要绑定宜搭连接器动作，请先用 `yida-connector` 创建连接器/动作，再把平台可访问的代理 URL 或设计器抓取到的原始配置放入该 JSON。

## 字段定义 JSON 格式

```json
[
  { "type": "TextField", "label": "姓名", "required": true },
  { "type": "SelectField", "label": "部门", "dataSource": [{"text":{"zh_CN":"技术部","en_US":"技术部","type":"i18n"},"value":"技术部"}] },
  { "type": "DateField", "label": "入职日期" },
  { "type": "TableField", "label": "费用明细", "children": [
    { "type": "TextField", "label": "项目" },
    { "type": "NumberField", "label": "金额" }
  ]}
]
```

### 字段属性

| 属性 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | String | 是 | 字段类型（见下方类型表） |
| `label` | String | 是 | 字段标签 |
| `required` | Boolean | 否 | 是否必填，默认 `false` |
| `placeholder` | String | 否 | 占位提示 |
| `behavior` | String | 否 | `NORMAL`（默认）/ `READONLY` / `HIDDEN` |
| `visibility` | String[] | 否 | `["PC", "MOBILE"]`（默认） |
| `dataSource` | Array | 条件必填 | 选项类字段必填 |
| `multiple` | Boolean | 否 | 是否多选 |
| `remoteDataSource` | Object | 否 | 选项类字段远程搜索数据源配置 |
| `children` | Object[] | 条件必填 | `TableField` 必填 |
| `associationForm` | Object | 条件必填 | `AssociationFormField` 必填 |

各字段类型默认属性参考 [表单字段属性参考](references/form-field-properties.md)。

## 修改定义 JSON 格式（update 模式）

```json
[
  { "action": "add", "field": { "type": "TextField", "label": "姓名", "required": true }, "after": "部门" },
  { "action": "delete", "label": "备注" },
  { "action": "update", "label": "年龄", "changes": { "required": true, "placeholder": "请输入年龄" } }
]
```

| 操作 | 必填属性 | 说明 |
|------|---------|------|
| `add` | `field.type`, `field.label` | 新增字段，`after`/`before` 指定位置 |
| `delete` | `label` | 删除字段 |
| `update` | `label`, `changes` | 修改字段属性，子表内字段需 `tableLabel` |

### update changes 支持的属性

`label`、`required`、`placeholder`、`dataSource`、`remoteDataSource`、`dataSourceUrl`、`searchConfig`、`multiple`、`behavior`、`visibility`、`innerAfter`（NumberField 单位）

## 支持的字段类型

| 字段类型 | 说明 | 特殊属性 |
|---------|------|----------|
| `TextField` | 单行文本 | — |
| `TextareaField` | 多行文本 | — |
| `RadioField` | 单选 | `dataSource` |
| `SelectField` | 下拉单选 | `dataSource` |
| `CheckboxField` | 多选 | `dataSource` |
| `MultiSelectField` | 下拉多选 | `dataSource` |
| `NumberField` | 数字 | — |
| `RateField` | 评分 | — |
| `DateField` | 日期 | — |
| `CascadeDateField` | 级联日期 | — |
| `EmployeeField` | 成员 | `multiple` |
| `DepartmentSelectField` | 部门 | `multiple` |
| `CountrySelectField` | 国家 | `multiple` |
| `AddressField` | 地址 | — |
| `AttachmentField` | 附件 | — |
| `ImageField` | 图片 | — |
| `TableField` | 子表 | `children` |
| `AssociationFormField` | 关联表单 | `associationForm` |
| `SerialNumberField` | 流水号 | 自动生成 |

## 注意事项

- 临时 JSON 文件用结构化文件写入工具写在 `<projectRoot>/.cache/openyida/<项目名或任务名>/` 目录中；不要写到仓库根目录、系统临时目录或 `.cache/` 顶层
- update 模式操作按顺序执行，注意依赖关系
- 字段匹配基于中文标签（`label.zh_CN`）
- 如需创建自定义展示页面（无字段），请使用 `yida-create-page`
- 如需美化表单详情页，先完成表单结构创建/更新，再使用 `yida-form-detail` 注入 Html 样式组件

## 异常处理

| 异常场景 | 处理方式 |
|---------|----------|
| create 返回失败 | 检查 appType 是否正确，确认登录态有效 |
| update 模式找不到字段 | 先用 `openyida get-schema` 确认字段标签（label）拼写正确 |
| 字段类型不支持 | 检查字段类型是否在支持的 19 种类型列表中 |
| 子表字段创建失败 | 确认 `children` 数组格式正确，子表字段不能嵌套子表 |
| 返回 JSON 中无 formUuid | 不要猜测 formUuid，重新执行命令获取 |
