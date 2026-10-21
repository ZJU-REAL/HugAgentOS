# 关联表单字段完整使用指南

关联表单字段（`AssociationFormField`）用于在当前表单中选择另一个表单的数据记录，并支持数据过滤和自动填充功能。

## 核心配置说明

**1. 基础配置**

```json
{
  "type": "AssociationFormField",
  "label": "关联字段名",
  "required": true,
  "multiple": false,
  "hasClear": true,
  "isShowSearchBar": true,
  "associationForm": {
    "formUuid": "FORM-XXX",
    "appType": "APP_xxx",
    "appName": "应用名称",
    "formTitle": "表单名称",
    "mainFieldId": "textField_xxx", // 主要信息
    "subFieldId": "selectField_mhokb6fa", // 次要信息组件 FieldId
    "subComponentName": "SelectField", // 次要信息组件类型
    "mainFieldLabel": "主显示字段标签",
    "mainComponentName": "TextField",
    "tableShowType": "custom",
    "customTableFields": ["textField_xxx", "numberField_xxx"]
  }
}
```

**2. 数据过滤规则（dataFilterRules）**

用于限制关联表单选择器中展示的数据范围，支持多条件组合筛选：

```json
{
  "dataFilterRules": {
    "condition": "AND",
    "rules": [
      {
        "id": "目标表单字段 fieldId",
        "name": "字段标签",
        "componentType": "TextField",
        "type": "TextField",
        "op": "contains",
        "filterType": "bizField",
        "extValue": "field",
        "ruleValue": "当前表单字段 fieldId",
        "value": "当前表单字段 fieldId",
        "valueType": "value",
        "ruleType": "rule_text",
        "ruleId": "item-唯一 ID"
      }
    ],
    "ruleId": "group-唯一 ID",
    "instanceFieldId": "",
    "version": "v2"
  }
}

```

**常用操作符（op）**：
- `eq` - 等于
- `is_not` - 不等于
- `contains` - 包含
- `not_contains` - 不包含

**extValue 说明**：
- `"field"` - 筛选值来源于当前表单的某个字段（`ruleValue` 填写当前表单的 fieldId）
- `"value"` - 筛选值为固定值（`ruleValue` 填写具体值）

**3. 数据回填规则（dataFillingRules）**

用于在选中关联表单记录后，自动将目标表单的字段值填充到当前表单的字段中：

```json
{
  "dataFillingRules": {
    "mainRules": [
      {
        "source": "目标表单字段 fieldId（数据来源）",
        "sourceType": "TextField",
        "target": "当前表单字段 fieldId（填充目标）",
        "targetType": "TextField"
      }
    ],
    "tableRules": [],
    "version": "v2"
  }
}
```

> ⚠️ **脚本内部实际生成的完整格式**（6 个字段，缺少任意一个回填不生效）：
>
> | 字段 | 说明 |
> |------|------|
> | `sourceFieldId` | 源字段 ID（与 `source` 相同，兼容旧格式） |
> | `targetFieldId` | 目标字段 ID（与 `target` 相同，兼容旧格式） |
> | `source` | 同 `sourceFieldId` |
> | `sourceType` | 源字段组件类型，如 `SerialNumberField`、`TextField` |
> | `target` | 同 `targetFieldId` |
> | `targetType` | 目标字段组件类型，如 `TextField`、`NumberField` |
>
> 配置时只需填写 `source/sourceType/target/targetType` 四个字段，脚本的 `normalizeFillingRules` 函数会自动补全 `sourceFieldId` 和 `targetFieldId`，并根据 fieldId 前缀自动推断组件类型（如 `serialNumberField_xxx` → `SerialNumberField`）。

**重要说明**：
- `mainRules` 用于主表字段之间的回填（最常用）
- `tableRules` 用于**子表填充子表**的回填（将关联表单的子表数据填充到当前表单的子表中）
- `source` 和 `target` 必须填写**字段 ID（fieldId）**，而非字段标签；支持 `@label:字段标签` 语法，脚本会自动解析为真实 fieldId
- `sourceType` 和 `targetType` 必须与目标字段的组件类型一致；如果不填，脚本会根据 fieldId 前缀自动推断
- 脚本会**自动推断**`supportDataFilling` 的值：当 `mainRules` 或 `tableRules` 非空时自动设为 `true`，否则为 `false`
- 如果配置了回填规则但实际不生效，请检查：
  1. `source` 和 `target` 的 fieldId 是否正确（可通过 get-schema 技能查看）
  2. `sourceType` 和 `targetType` 是否与字段类型一致
  3. `dataFillingRules` 是否被正确设置到 `props.dataFillingRules`（而非只写在 `associationForm` 对象内）
  4. 关联表单字段的 Schema 节点**顶层**是否有 `fieldId` 属性（宜搭回填引擎依赖顶层 `fieldId` 识别字段，仅在 `props` 内有 `fieldId` 不生效）

**子表填充子表（tableRules）格式**：

```json
{
  "dataFillingRules": {
    "mainRules": [
      {
        "source": "目标表单主表字段 fieldId",
        "sourceType": "TextField",
        "target": "@label:当前表单主表字段标签",
        "targetType": "TextField"
      }
    ],
    "tableRules": [
      {
        "tableId": "被关联表单（源表）的子表字段 fieldId",
        "rules": [
          {
            "source": "目标表单子表字段 fieldId",
            "sourceType": "TextareaField",
            "target": "@label:当前表单子表内字段标签",
            "targetType": "TextField"
          }
        ],
        "filters": ""
      }
    ],
    "version": "v2"
  }
}
```

**tableRules 字段说明**：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `tableId` | String | 是 | ⚠️ **被关联表单（源表）的子表字段 fieldId**，不是当前表单的子表 fieldId |
| `rules` | Array | 是 | 子表内的字段映射规则数组 |
| `rules[].source` | String | 是 | 关联表单子表中的源字段 fieldId |
| `rules[].target` | String | 是 | 当前表单子表中的目标字段 fieldId，支持 `@label:字段标签` 语法 |
| `rules[].sourceType` | String | 是 | 源字段类型，如 `TextareaField`、`NumberField` 等 |
| `rules[].targetType` | String | 是 | 目标字段类型，需与目标字段实际类型一致 |
| `filters` | String | 否 | 过滤条件，通常为空字符串 |

**注意事项**：
- `tableRules` 用于实现"选择关联表单记录后，将其子表数据自动填充到当前表单的子表中"
- **`tableId` 必须填写被关联表单（源表）的子表 fieldId**，宜搭通过它在源表 Schema 中定位子表数据；填写当前表单的子表 fieldId 会导致 `getInstMultiSubTableDatas` 接口报 500 错误
- 每个 `tableRule` 对应源表的一个子表，可以配置多个子表的回填规则
- 子表内的 `target` 同样支持 `@label:字段标签` 语法，脚本会自动解析为 fieldId
- 确保 `rules[].source` 是源表子表内的字段 fieldId，`rules[].target` 是当前表单子表内的字段 fieldId

#### 完整使用示例

**场景**：创建任务表，关联人员信息表，选择人员后自动填充年龄

**步骤 1：创建人员信息表**

```json
[
  {
    "type": "TextField",
    "label": "姓名",
    "required": true,
    "placeholder": "请输入姓名"
  },
  {
    "type": "NumberField",
    "label": "年龄",
    "required": true,
    "placeholder": "请输入年龄"
  }
]
```

执行创建后，记录返回的 `formUuid` 和各字段 `fieldId`：
- formUuid: `FORM-XXX`
- 姓名 fieldId: `textField_xxx`
- 年龄 fieldId: `numberField_xxx`

**步骤 2：创建任务表（带关联表单和自动填充）**

```json
[
  {
    "type": "TextField",
    "label": "任务名称",
    "required": true,
    "placeholder": "请输入任务名称"
  },
  {
    "type": "AssociationFormField",
    "label": "选择人员",
    "required": true,
    "multiple": false,
    "hasClear": true,
    "isShowSearchBar": true,
    "associationForm": {
      "formUuid": "FORM-XXX",
      "appType": "APP_xxx",
      "appName": "测试应用",
      "formTitle": "人员信息表",
      "mainFieldId": "textField_xxx",
      "mainFieldLabel": "姓名",
      "mainComponentName": "TextField",
      "tableShowType": "custom",
      "customTableFields": ["textField_xxx", "numberField_xxx"],
      "dataFillingRules": {
        "mainRules": [
          {
            "source": "numberField_xxx",
            "sourceType": "NumberField",
            "target": "@label:年龄",
            "targetType": "NumberField"
          }
        ],
        "tableRules": [],
        "version": "v2"
      }
    }
  },
  {
    "type": "NumberField",
    "label": "年龄",
    "required": true,
    "placeholder": "选择人员后自动填充"
  }
]
```

**注意事项**：
1. 必须先创建被关联的表单（人员信息表），获取其 `formUuid` 和字段 `fieldId`（通过 `get-schema` 技能查看）
2. `dataFillingRules` 中的 `target` 使用 `@label:年龄` 语法，脚本会自动将其解析为当前表单中「年龄」字段的真实 fieldId，**无需手动查找**
3. `source` 填写被关联表单（人员信息表）中年龄字段的真实 fieldId
4. 脚本会自动设置 `supportDataFilling: true`，无需手动配置

#### 子表填充子表示例

**场景**：创建订单表，关联商品表，选择商品后将商品明细（子表）自动填充到订单明细（子表）

**步骤 1：创建商品表（含商品明细子表）**

```json
[
  {
    "type": "TextField",
    "label": "商品名称",
    "required": true
  },
  {
    "type": "NumberField",
    "label": "商品单价",
    "required": true
  },
  {
    "type": "TableField",
    "label": "规格明细",
    "children": [
      { "type": "TextField", "label": "规格名称" },
      { "type": "NumberField", "label": "规格价格" },
      { "type": "NumberField", "label": "库存数量" }
    ]
  }
]
```

执行创建后，记录返回的 `formUuid` 和各字段 `fieldId`：
- formUuid: `FORM-XXX`
- 商品名称 fieldId: `textField_xxx`
- 商品单价 fieldId: `numberField_xxx`
- 规格明细（子表）fieldId: `tableField_xxx`
- 规格名称（子表内）fieldId: `textField_xxx`
- 规格价格（子表内）fieldId: `numberField_xxx`
- 库存数量（子表内）fieldId: `numberField_xxx`

**步骤 2：创建订单表（带子表和关联表单）**

```json
[
  {
    "type": "TextField",
    "label": "订单编号",
    "required": true
  },
  {
    "type": "AssociationFormField",
    "label": "选择商品",
    "required": true,
    "associationForm": {
      "formUuid": "FORM-XXX",
      "appType": "APP_xxx",
      "appName": "商品管理",
      "formTitle": "商品信息表",
      "mainFieldId": "textField_xxx",
      "mainFieldLabel": "商品名称",
      "mainComponentName": "TextField",
      "tableShowType": "custom",
      "customTableFields": ["textField_xxx", "numberField_xxx"],
      "dataFillingRules": {
        "mainRules": [
          {
            "source": "numberField_xxx",
            "sourceType": "NumberField",
            "target": "@label:商品单价",
            "targetType": "NumberField"
          }
        ],
        "tableRules": [
          {
            "tableId": "@label:订单明细",
            "rules": [
              {
                "source": "textField_xxx",
                "sourceType": "TextField",
                "target": "@label:规格",
                "targetType": "TextField"
              },
              {
                "source": "numberField_xxx",
                "sourceType": "NumberField",
                "target": "@label:单价",
                "targetType": "NumberField"
              },
              {
                "source": "numberField_xxx",
                "sourceType": "NumberField",
                "target": "@label:数量",
                "targetType": "NumberField"
              }
            ],
            "filters": ""
          }
        ],
        "version": "v2"
      }
    }
  },
  {
    "type": "NumberField",
    "label": "商品单价",
    "required": true
  },
  {
    "type": "TableField",
    "label": "订单明细",
    "children": [
      { "type": "TextField", "label": "规格" },
      { "type": "NumberField", "label": "单价" },
      { "type": "NumberField", "label": "数量" }
    ]
  }
]
```

**关键点说明**：
1. `tableRules` 中的 `tableId` 填写的是**被关联表单（商品表）的子表 fieldId**（即「规格明细」子表的 fieldId），而非当前表单（订单表）的子表 fieldId。宜搭通过此 fieldId 在源表 Schema 中定位子表数据，填错会导致 `getInstMultiSubTableDatas` 接口报 500 错误
2. `tableRules` 中的每个 `rule.target` 支持 `@label:字段标签` 语法，用于定位当前表单子表内的字段
3. 选择商品后，商品表的「规格明细」子表数据会自动填充到订单表的「订单明细」子表中
4. 可以配置多个 `tableRule` 来实现多子表的数据回填

#### 常见问题排查

**问题 1：关联表单选择后没有自动填充**
- 检查 `dataFillingRules.mainRules` 是否非空
- 检查 `source` 和 `target` 的 fieldId 是否正确（可通过 get-schema 技能查看）
- 检查 `sourceType` 和 `targetType` 是否与字段类型一致
- 确认 `props.supportDataFilling` 是否为 `true`
- 确认每条规则同时包含 `sourceFieldId/targetFieldId/source/sourceType/target/targetType` 6 个字段（缺少任意一个不生效）
- 确认关联表单字段的 Schema 节点**顶层**有 `fieldId` 属性（宜搭回填引擎依赖顶层 `fieldId`，仅在 `props` 内有 `fieldId` 不生效）

**问题 2：子表填充子表不生效**
- 检查 `tableRules` 是否为数组且非空
- 检查 `tableId` 是否正确（⚠️ 应为**被关联表单（源表）的子表 fieldId**，不是当前表单的子表 fieldId）
- 检查 `tableRules[].rules` 是否为数组且非空
- 检查子表内的 `source` 和 `target` fieldId 是否正确
- 确认 `tableId` 对应的子表字段在被关联表单中存在
- 查看控制台输出是否有 `📋 处理子表回填规则` 的日志

**问题 3：关联表单选择器中显示的数据不对**
- 检查 `dataFilterRules` 配置是否正确
- 检查 `ruleValue` 指向的当前表单字段是否有值
- 检查 `op` 操作符是否合适

**问题 4：关联表单选择器中显示的字段不对**
- 检查 `mainFieldId` 是否为主显示字段的 fieldId
- 检查 `customTableFields` 是否包含需要展示的字段 fieldId
- 检查 `tableShowType` 是否为 `"custom"`（如需自定义展示）
#### 完整使用示例

```json
{
  "type": "AssociationFormField",
  "label": "选择门店人员",
  "required": true,
  "multiple": false,
  "associationForm": {
    "formUuid": "FORM-447E4C1A330B4D8E975A49D6B8514DBFCLT5",
    "appType": "APP_U4PQPSBLV0DI311TCFY7",
    "appName": "一群",
    "formTitle": "导购信息",
    "mainFieldId": "textField_mi2zgd95",
    "mainFieldLabel": "姓名",
    "mainComponentName": "TextField",
    "tableShowType": "custom",
    "customTableFields": [
      "textField_mi2zgd95",
      "selectField_mhvrw9zj",
      "employeeField_mii8l25k"
    ],
    "subFieldId": "selectField_mhokb6fa",
    "subComponentName": "SelectField",
    "dataFilterRules": {
      "condition": "AND",
      "rules": [
        {
          "id": "textField_mhvrw9zt",
          "name": "门店编码",
          "componentType": "TextField",
          "type": "TextField",
          "op": "contains",
          "filterType": "bizField",
          "extValue": "field",
          "ruleValue": "multiSelectField_mksf6jwb",
          "value": "multiSelectField_mksf6jwb",
          "valueType": "value",
          "ruleType": "rule_text",
          "ruleId": "item-唯一ID"
        }
      ],
      "ruleId": "group-唯一ID",
      "instanceFieldId": "",
      "version": "v2"
    },
    "dataFillingRules": {
      "mainRules": [
        {
          "source": "employeeField_mhok3twl",
          "sourceType": "EmployeeField",
          "target": "employeeField_mff5ro7o",
          "targetType": "EmployeeField"
        }
      ],
      "tableRules": [],
      "version": "v2"
    },
    "orderConfig": [
      { "fieldId": "textField_mhvrw9zt", "order": "desc" }
    ]
  }
}
```
