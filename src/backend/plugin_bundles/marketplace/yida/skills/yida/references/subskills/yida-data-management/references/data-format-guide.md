# Data Format Guide

## 基础规则

- `searchFieldJson` 传字符串，不直接传对象
- `formDataJson` 传字符串，不直接传对象
- `updateFormDataJson` 传字符串，不直接传对象
- `dynamicOrder` 传字符串，不直接传对象
- `DateField` / `CascadeDateField` 的保存、更新和测试数据值必须使用**13 位毫秒时间戳**，不要传日期字符串

## 查询条件格式

```json
[
  {
    "key": "字段ID",
    "value": "搜索值",
    "type": "字段类型",
    "operator": "操作符",
    "componentName": "组件名称"
  }
]
```

示例：

```bash
--search-json '[{"key":"textField_xxx","value":"测试","type":"TEXT","operator":"eq","componentName":"TextField"}]'
```

## 保存 / 更新格式

```json
{
  "textField_xxx": "文本",
  "numberField_xxx": 10,
  "dateField_xxx": 1719705600000,
  "employeeField_xxx": ["2212173665758008"]
}
```

更新时只传要修改的字段：

```json
{
  "textField_xxx": "更新后的值"
}
```

## 排序格式

```json
{"numberField_1ac":"+"}
```

## 常见字段值格式

| 组件类型 | 查询格式 | 保存 / 更新格式 |
| --- | --- | --- |
| 单行 / 多行文本 | `"文本"` | `"文本"` |
| 数字 | `["1","10"]` 或单值 | `1` |
| 单选 | `"选项一"` | `"选项一"` |
| 多选 | `["选项一"]` | `["选项一","选项二"]` |
| 日期 | `[开始毫秒时间戳,结束毫秒时间戳]` | `13 位毫秒时间戳` |
| 成员 | `["userId"]` | `["userId"]` |
| 部门 | `1123456` 或 `["1123456"]` | `["1123456"]` |
| 城市 | `[省ID,市ID,区ID]` | `[省ID,市ID,区ID]` |
| 子表单 | `"模糊搜索文本"` | `[{"textField_xxx":"值"}]` |
| 关联表单 | 不支持直接查询 | `[{"appType":"xxx","formUuid":"xxx","instanceId":"xxx"}]` |

## 日期字段（DateField / CascadeDateField）

生成测试数据、录入或更新数据时，日期字段必须写入毫秒级时间戳：

```json
{
  "dateField_xxx": 1719705600000,
  "cascadeDateField_xxx": [1719705600000, 1722384000000]
}
```

不要写成日期字符串：

```json
{
  "dateField_xxx": "2024-06-30"
}
```

可用 JavaScript 转换固定日期：

```js
new Date('2024-06-30T00:00:00+08:00').getTime()
```

如果传 `YYYY-MM-DD`、`YYYY-MM-DD HH:mm:ss` 或 ISO 字符串，宜搭可能保存失败、保存为空，或导致报表/筛选异常。

## 关联表单字段（AssociationFormField）

关联表单字段用于引用其他表单的数据记录，数据格式较为特殊。

### 保存格式

必须传入**数组对象**，每个对象包含三个必填字段：

```json
[
  {
    "appType": "APP_xxx",
    "formUuid": "FORM-xxx",
    "instanceId": "FINST-xxx"
  }
]
```

| 字段 | 说明 |
| --- | --- |
| `appType` | 被关联表单所属应用的 appType |
| `formUuid` | 被关联表单的 formUuid |
| `instanceId` | 被关联数据的 formInstId（注意：字段名是 instanceId，不是 formInstId） |

### 示例

```bash
# 1. 先查询被关联表单获取 formInstId
openyida data query form APP_xxx FORM-客户表 --size 1
# 返回: formInstId: "FINST-ABC123"

# 2. 创建带关联的数据
openyida data create form APP_xxx FORM-商机表 --data-json '{
  "textField_xxx": "商机名称",
  "associationFormField_xxx": [{"appType":"APP_xxx","formUuid":"FORM-客户表","instanceId":"FINST-ABC123"}]
}'
```

### 查询返回格式

查询时返回的 `formData` 中，关联表单字段以 `_id` 后缀显示：

```json
{
  "associationFormField_xxx_id": "\"[{...}]\""
}
```

### 注意事项

- **instanceId 命名**：保存时字段名必须是 `instanceId`，不是 `formInstId`
- **数组格式**：即使只关联一条数据，也必须使用数组格式 `[{...}]`
- **三字段必填**：`appType`、`formUuid`、`instanceId` 缺一不可，否则返回参数校验失败
- **跨应用关联**：如果关联的是其他应用的表单，需要使用对应应用的 appType
- **API 限制**：部分宜搭环境可能限制通过 API 写入关联表单数据，建议在表单界面手动测试确认

## 实现建议

- 统一把 CLI 传入的 JSON 参数当作字符串接收
- 在发送请求前再做 `json.dumps(...)` 或直接透传字符串
- `currentPage` 从 `1` 开始
- `pageSize` 建议不超过 `100`
