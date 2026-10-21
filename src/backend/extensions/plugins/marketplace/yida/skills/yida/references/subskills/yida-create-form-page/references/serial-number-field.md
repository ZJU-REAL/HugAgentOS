## SerialNumberField 流水号规则详解

### 默认场景（无自定义需求）

只有一条 `autoCount` 规则，4 位自增，从 1 开始，不重置：

```json
{
  "serialNumberRule": [
    {
      "resetPeriod": "noClean",
      "dateFormat": "yyyyMMdd",
      "ruleType": "autoCount",
      "timeZone": "+8",
      "__sid": "item_auto_count",
      "__hide_delete__": true,
      "__sid__": "serial_auto_count",
      "digitCount": "4",
      "isFixed": true,
      "initialValue": 1,
      "content": "",
      "formField": ""
    }
  ],
  "serialNumPreview": "0001"
}
```

> ⚠️ 注意：默认场景的 `autoCount` 规则 `__hide_delete__` 必须为 `true`（不可删除），`digitCount` 为字符串 `"4"`。

### 自定义场景（多规则组合）

支持以下 4 种规则类型，可任意组合排列：

| `ruleType` | 说明 | 关键字段 |
| --- | --- | --- |
| `autoCount` | 自动计数（自增数字） | `digitCount`（位数，字符串）、`initialValue`（起始值）、`resetPeriod`（重置周期） |
| `character` | 固定字符 | `content`（固定字符串内容） |
| `date` | 提交日期 | `dateFormat`（日期格式，如 `"yy"`、`"yyyyMMdd"`） |
| `form` | 取表单字段值 | `formField`（字段 fieldId） |

**自定义规则通用结构**：

```json
{
  "resetPeriod": "noClean",
  "dateFormat": "yyyyMMdd",
  "ruleType": "<类型>",
  "timeZone": "+8",
  "__sid": "item_<唯一id>",
  "__hide_delete__": false,
  "__sid__": "serial_<唯一id>",
  "digitCount": 4,
  "isFixed": true,
  "initialValue": 1,
  "content": "<固定字符，仅 character 类型填写>",
  "formField": "<字段fieldId，仅 form 类型填写>",
  "isFixedTips": "",
  "resetPeriodTips": ""
}
```

> ⚠️ 注意：自定义规则中 `digitCount` 为**数字**类型（不是字符串），`__hide_delete__` 为 `false`；而默认 `autoCount` 规则的 `digitCount` 是**字符串** `"4"`，`__hide_delete__` 为 `true`。

**`resetPeriod` 重置周期可选值**：

| 值 | 说明 |
| --- | --- |
| `"noClean"` | 不重置（永久自增） |
| `"day"` | 每天重置 |
| `"month"` | 每月重置 |
| `"year"` | 每年重置 |

**`dateFormat` 日期格式可选值**（`date` 类型规则）：

| 值 | 示例 |
| --- | --- |
| `"yy"` | `26`（年份后两位） |
| `"yyyy"` | `2026` |
| `"yyyyMM"` | `202603` |
| `"yyyyMMdd"` | `20260311` |

**示例：`YY-提交日期-自动计数4位` 的规则配置**：

```json
{
  "serialNumberRule": [
    {
      "ruleType": "character",
      "content": "YY",
      "dateFormat": "yyyyMMdd", "timeZone": "+8",
      "resetPeriod": "noClean", "digitCount": 4,
      "isFixed": true, "initialValue": 1,
      "isFixedTips": "", "resetPeriodTips": "",
      "formField": "",
      "__sid": "item_char_yy", "__sid__": "serial_char_yy", "__hide_delete__": false
    },
    {
      "ruleType": "date",
      "content": "",
      "dateFormat": "yy", "timeZone": "+8",
      "resetPeriod": "noClean", "digitCount": 4,
      "isFixed": true, "initialValue": 1,
      "isFixedTips": "", "resetPeriodTips": "",
      "formField": "",
      "__sid": "item_date_yy", "__sid__": "serial_date_yy", "__hide_delete__": false
    },
    {
      "ruleType": "autoCount",
      "content": "",
      "dateFormat": "yyyyMMdd", "timeZone": "+8",
      "resetPeriod": "noClean", "digitCount": 4,
      "isFixed": true, "initialValue": 1,
      "isFixedTips": "", "resetPeriodTips": "",
      "formField": "",
      "__sid": "item_auto", "__sid__": "serial_auto", "__hide_delete__": true
    }
  ],
  "serialNumPreview": "YY260001"
}
```

### formula 构建规则

`formula` 必须是**对象格式**（不是字符串），`expression` 的最后一个参数是 `{ "type": "custom", "value": <serialNumberRule数组> }` 的 JSON 字符串，需对双引号转义：

```javascript
var ruleJson = JSON.stringify({ type: 'custom', value: serialNumberRule });
var escapedRuleJson = ruleJson.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
var expression = 'SERIALNUMBER("' + corpId + '", "' + appType + '", "' + formUuid + '", "' + fieldId + '", "' + escapedRuleJson + '")';
obj.formula = { expression: expression };
```
