# 宜搭查询条件编写指南

> 本指南供 AI 助手编写宜搭表单数据查询条件时参考

## 快速开始

### 基本格式

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

**重要**：`searchFieldJson` 是一个 **JSON 数组的字符串形式**，外层用单引号包裹。

---

## 字段类型速查表

| 组件名称 | 组件英文 | type | 常用操作符 |
|---------|---------|------|-----------|
| 单行文本 | TextField | TEXT | eq, like |
| 多行文本 | TextareaField | TEXT | eq, like |
| 数值 | NumberField | DOUBLE | gt, ge, lt, le, between |
| 评分 | RateField | DOUBLE | gt, ge, lt, le, between |
| 日期 | DateField | DOUBLE | gt, ge, lt, le, between |
| 日期区间 | CascadeDateField | DOUBLE | 无（默认） |
| 单选 | RadioField | ARRAY | eq, contains |
| 下拉单选 | SelectField | ARRAY | eq, contains |
| 复选 | CheckboxField | ARRAY | eq, contains |
| 下拉复选 | MultiSelectField | ARRAY | eq, contains |
| 级联选择 | CascadeSelectField | STRING | eq, contains |
| 图片 | ImageField | TEXT | like |
| 附件 | AttachmentField | TEXT | like |
| 成员 | EmployeeField | STRING | eq, contains |
| 子表单 | TableField | TEXT | contains |

---

## 按字段类型编写

### 1. 文本类（单行/多行）

**操作符**：
- `eq` - 完全相等
- `like` - 模糊匹配（包含）

**示例**：
```bash
# 单行文本等于 "A"
--search-json '[{"key":"textField_xxx","value":"A","type":"TEXT","operator":"eq","componentName":"TextField"}]'

# 多行文本包含 "测试"
--search-json '[{"key":"textareaField_xxx","value":"测试","type":"TEXT","operator":"like","componentName":"TextareaField"}]'
```

---

### 2. 数值类（数值/评分）

**操作符**：
- `gt` - 大于
- `ge` - 大于等于
- `lt` - 小于
- `le` - 小于等于
- `between` - 介于两者之间

**注意**：
- type 必须是 `"DOUBLE"`
- value 是数字（不加引号）
- between 的 value 是数组 `[最小值, 最大值]`

**示例**：
```bash
# 数值大于 10
--search-json '[{"key":"numberField_xxx","value":10,"type":"DOUBLE","operator":"gt","componentName":"NumberField"}]'

# 评分介于 2 到 4 之间
--search-json '[{"key":"rateField_xxx","value":[2,4],"type":"DOUBLE","operator":"between","componentName":"RateField"}]'
```

---

### 3. 日期类

**操作符**：同数值类

**注意**：
- 时间戳是**毫秒级**（13位数字）
- 可使用 `Date.now()` 获取当前时间戳

**示例**：
```bash
# 日期大于 2022-04-06
--search-json '[{"key":"dateField_xxx","value":1649174400000,"type":"DOUBLE","operator":"gt","componentName":"DateField"}]'

# 日期区间（无操作符）
--search-json '[{"key":"cascadeDateField_xxx","value":[[1649433600000,1651593599000],[1649260800000,1652198399000]],"type":"DOUBLE","componentName":"CascadeDateField"}]'
```

**时间戳转换**：
```javascript
// JavaScript
new Date("2024-01-01").getTime()  // 1704067200000

// Python
import time
int(time.mktime(time.strptime("2024-01-01", "%Y-%m-%d"))) * 1000
```

---

### 4. 选择类（单选/复选/下拉）

**操作符**：
- `eq` - 等于（单值）
- `contains` - 包含（多值）

**注意**：
- type 是 `"ARRAY"`
- eq 时 value 是字符串 `"选项一"`
- contains 时 value 是数组 `["选项一","选项二"]`

**示例**：
```bash
# 单选等于 "选项一"
--search-json '[{"key":"radioField_xxx","value":"选项一","type":"ARRAY","operator":"eq","componentName":"RadioField"}]'

# 复选包含 "选项一" 或 "选项二"
--search-json '[{"key":"checkboxField_xxx","value":["选项一","选项二"],"type":"ARRAY","operator":"contains","componentName":"CheckboxField"}]'
```

---

### 5. 级联选择

**注意**：
- type 是 `"STRING"`
- value 是数组 `["一级选项","二级选项"]`

**示例**：
```bash
# 级联选择等于 ["product","product_a"]
--search-json '[{"key":"cascadeSelectField_xxx","value":["product","product_a"],"type":"STRING","operator":"eq","componentName":"CascadeSelectField"}]'
```

---

### 6. 文件类（图片/附件）

**操作符**：`like`

**注意**：
- type 是 `"TEXT"`
- 搜索文件路径或应用 ID

**示例**：
```bash
# 图片包含 fileUpload 路径
--search-json '[{"key":"imageField_xxx","value":"fileUpload/","type":"TEXT","operator":"like","componentName":"ImageField"}]'

# 附件包含应用 ID
--search-json '[{"key":"attachmentField_xxx","value":"APP_XXX","type":"TEXT","operator":"like","componentName":"AttachmentField"}]'
```

---

### 7. 成员类

**操作符**：
- `eq` - 等于（单用户）
- `contains` - 包含（多用户）

**注意**：
- type 是 `"STRING"`
- value 是钉钉 userId
- contains 时 value 需**字符串化数组**

**示例**：
```bash
# 成员等于某 userId
--search-json '[{"key":"employeeField_xxx","value":"2212173665758008","type":"STRING","operator":"eq","componentName":"EmployeeField"}]'

# 成员包含多个 userId（注意 value 是字符串）
--search-json '[{"key":"employeeField_xxx","value":"[\"2212173665758008\",\"2212173665758009\"]","type":"STRING","operator":"contains","componentName":"EmployeeField"}]'
```

---

### 8. 子表单

**操作符**：`contains`

**示例**：
```bash
# 子表单包含关键词
--search-json '[{"key":"tableField_xxx","value":"关键词","type":"TEXT","operator":"contains","componentName":"TableField"}]'
```

---

### 9. 子表单内部组件

**注意**：
- 需添加 `parentId` 字段，值为子表单组件的 ID
- 其他属性同主表单组件

**示例**：
```bash
# 子表单内的单行文本
--search-json '[{"key":"textField_xxx","value":"测试","type":"TEXT","operator":"like","componentName":"TextField","parentId":"tableField_xxx"}]'
```

---

## 多条件组合（AND 关系）

在数组中添加多个条件对象，各条件之间是 AND 关系：

```bash
--search-json '[
  {"key":"textField_xxx","value":"A","type":"TEXT","operator":"eq","componentName":"TextField"},
  {"key":"numberField_xxx","value":10,"type":"DOUBLE","operator":"gt","componentName":"NumberField"}
]'
```

---

## 完整示例

### 示例 1：简单查询
```bash
openyida data query form \
  "APP_CQ2P5NRFI5L1D6PB8Q7J" \
  "FORM-035536EBA11447158458DFF86E7867E7WIW0" \
  --search-json '[{"key":"textField_wduvybsi","value":"A","type":"TEXT","operator":"eq","componentName":"TextField"}]'
```

### 示例 2：数值范围查询
```bash
openyida data query form \
  "APP_XXX" \
  "FORM_XXX" \
  --search-json '[{"key":"numberField_xxx","value":[10,100],"type":"DOUBLE","operator":"between","componentName":"NumberField"}]'
```

### 示例 3：日期范围查询
```bash
openyida data query form \
  "APP_XXX" \
  "FORM_XXX" \
  --search-json '[{"key":"dateField_xxx","value":[1704067200000,1706745600000],"type":"DOUBLE","operator":"between","componentName":"DateField"}]'
```

### 示例 4：多条件组合查询
```bash
openyida data query form \
  "APP_XXX" \
  "FORM_XXX" \
  --search-json '[{"key":"textField_xxx","value":"A","type":"TEXT","operator":"eq","componentName":"TextField"},{"key":"numberField_xxx","value":10,"type":"DOUBLE","operator":"gt","componentName":"NumberField"},{"key":"dateField_xxx","value":1704067200000,"type":"DOUBLE","operator":"gt","componentName":"DateField"}]'
```

---

## 常见错误

| 错误 | 原因 | 解决 |
|------|------|------|
| field is null | 字段 ID 不存在 | 检查字段 ID 是否正确 |
| 暂时不支持其他类型Field | type 值错误 | 检查 type 是否为正确的值 |
| csrf校验失败 | 登录态过期 | 重新登录获取新 Cookie |
| 平台未知异常 | 应用/表单不存在 | 检查 appType 和 formUuid |

---

## 如何获取字段 ID

1. 在宜搭表单设计器中查看字段属性
2. 使用 `openyida get-schema` 命令获取表单结构
3. 查看已有数据的 formData 字段名

```bash
# 获取表单 Schema
openyida get-schema "APP_XXX" "FORM_XXX"
```

---

*文档版本：1.0*
*最后更新：2024-03-18*
