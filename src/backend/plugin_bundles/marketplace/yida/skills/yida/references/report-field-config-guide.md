# 宜搭报表字段配置指南

本文档详细说明宜搭报表中不同字段类型的配置规则,避免常见的配置错误。

## 核心规则

### 1. 字段类型与 fieldCode 格式

| 字段类型 | fieldCode 格式 | dataType | 说明 |
|---------|---------------|----------|------|
| **TextField** (单行文本) | `textField_xxx` | `STRING` | 直接使用原始 fieldCode |
| **TextareaField** (多行文本) | `textareaField_xxx` | `STRING` | 直接使用原始 fieldCode |
| **NumberField** (数字) | `numberField_xxx` | `DOUBLE` | 直接使用原始 fieldCode |
| **SelectField** (下拉单选) | `selectField_xxx_value` | `STRING` | **必须加 `_value` 后缀** |
| **RadioField** (单选) | `radioField_xxx_value` | `STRING` | **必须加 `_value` 后缀** |
| **MultiSelectField** (下拉多选) | `multiSelectField_xxx_value` | `STRING` | **必须加 `_value` 后缀** |
| **CheckboxField** (多选) | `checkboxField_xxx_value` | `STRING` | **必须加 `_value` 后缀** |
| **DateField** (日期) | `dateField_xxx` | `DATE` | 直接使用原始 fieldCode,**不要拆分** |
| **EmployeeField** (成员) | `employeeField_xxx` | `STRING` | 直接使用原始 fieldCode |
| **DepartmentSelectField** (部门) | `departmentSelectField_xxx` | `STRING` | 直接使用原始 fieldCode |

### 2. 关键注意事项

#### SelectField/RadioField/MultiSelectField/CheckboxField 必须加 `_value` 后缀

**错误示例**:
```json
{
  "fieldCode": "selectField_k2ak55yx3",  //❌错误
  "aliasName": "行业",
  "dataType": "STRING",
  "aggregateType": "NONE"
}
```

**正确示例**:
```json
{
  "fieldCode": "selectField_k2ak55yx3_value",  // ✅ 正确
  "aliasName": "行业",
  "dataType": "STRING",
  "aggregateType": "NONE"
}
```

#### DateField 不要拆分为年月日时分秒

**错误示例**:
```json
//❌错误:不要拆分为6个字段
{
  "fieldCode": "dateField_k2ak7mxcx_year",
  "aliasName": "年",
  "dataType": "DATE",
  "aggregateType": "NONE"
}
```

**正确示例**:
```json
// ✅ 正确:直接使用原始 fieldCode
{
  "fieldCode": "dateField_k2ak7mxcx",
  "aliasName": "创建日期",
  "dataType": "DATE",
  "aggregateType": "NONE"
}
```

#### 聚合类型与 dataType 的对应关系

当使用聚合函数时,`dataType` 会自动转换为 `DOUBLE`:

| aggregateType | 实际 dataType | 说明 |
|--------------|--------------|------|
| `COUNT` | `DOUBLE` | 计数结果始终是数值 |
| `SUM` | `DOUBLE` | 求和结果始终是数值 |
| `AVG` | `DOUBLE` | 平均值结果始终是数值 |
| `MAX` | `DOUBLE` | 最大值结果始终是数值 |
| `MIN` | `DOUBLE` | 最小值结果始终是数值 |
| `COUNT_DISTINCT` | `DOUBLE` | 去重计数结果始终是数值 |
| `NONE` | 保持原类型 | 不进行聚合,保持字段原始类型 |

## 完整配置示例

### 示例 1: CRM 客户报表

```json
[
  {
    "type": "indicator",
    "title": "客户总数",
    "cubeCode": "FORM-D399CAC7A21D4F43B8ED222FF67F96EESK3J",
    "kpi": [
      {
        "fieldCode": "textField_k2ak1lrej",
        "aliasName": "客户名称",
        "dataType": "STRING",
        "aggregateType": "COUNT"
      }
    ],
    "w": 12,
    "h": 6
  },
  {
    "type": "pie",
    "title": "客户行业分布",
    "cubeCode": "FORM-D399CAC7A21D4F43B8ED222FF67F96EESK3J",
    "xField": {
      "fieldCode": "selectField_k2ak55yx3_value",
      "aliasName": "行业",
      "dataType": "STRING",
      "aggregateType": "NONE"
    },
    "yField": [
      {
        "fieldCode": "textField_k2ak1lrej",
        "aliasName": "客户名称",
        "dataType": "STRING",
        "aggregateType": "COUNT"
      }
    ],
    "w": 6,
    "h": 20
  },
  {
    "type": "bar",
    "title": "客户来源渠道",
    "cubeCode": "FORM-D399CAC7A21D4F43B8ED222FF67F96EESK3J",
    "xField": {
      "fieldCode": "selectField_k2ak68ptp_value",
      "aliasName": "客户来源",
      "dataType": "STRING",
      "aggregateType": "NONE"
    },
    "yField": [
      {
        "fieldCode": "textField_k2ak1lrej",
        "aliasName": "客户名称",
        "dataType": "STRING",
        "aggregateType": "COUNT"
      }
    ],
    "w": 6,
    "h": 20
  },
  {
    "type": "line",
    "title": "客户创建趋势",
    "cubeCode": "FORM-D399CAC7A21D4F43B8ED222FF67F96EESK3J",
    "xField": {
      "fieldCode": "dateField_k2ak7mxcx",
      "aliasName": "创建日期",
      "dataType": "DATE",
      "aggregateType": "NONE"
    },
    "yField": [
      {
        "fieldCode": "textField_k2ak1lrej",
        "aliasName": "客户名称",
        "dataType": "STRING",
        "aggregateType": "COUNT"
      }
    ],
    "w": 12,
    "h": 20
  },
  {
    "type": "table",
    "title": "客户列表",
    "cubeCode": "FORM-D399CAC7A21D4F43B8ED222FF67F96EESK3J",
    "columnFields": [
      {
        "fieldCode": "textField_k2ak1lrej",
        "aliasName": "客户名称",
        "dataType": "STRING",
        "aggregateType": "NONE"
      },
      {
        "fieldCode": "textField_k2ak2gbo8",
        "aliasName": "联系人",
        "dataType": "STRING",
        "aggregateType": "NONE"
      },
      {
        "fieldCode": "textField_k2ak3dlc4",
        "aliasName": "电话",
        "dataType": "STRING",
        "aggregateType": "NONE"
      },
      {
        "fieldCode": "selectField_k2ak55yx3_value",
        "aliasName": "行业",
        "dataType": "STRING",
        "aggregateType": "NONE"
      },
      {
        "fieldCode": "selectField_k2ak68ptp_value",
        "aliasName": "客户来源",
        "dataType": "STRING",
        "aggregateType": "NONE"
      },
      {
        "fieldCode": "dateField_k2ak7mxcx",
        "aliasName": "创建日期",
        "dataType": "DATE",
        "aggregateType": "NONE"
      }
    ],
    "w": 12,
    "h": 40
  }
]
```

## 报表布局最佳实践

> **重要**：报表使用 **6 列栅格系统**（w=6 表示占满整行），代码已内置 `getDefaultLayout()` 函数，会根据图表类型自动分配最佳默认布局。**通常无需手动指定 `w`/`h`**，只需关注图表类型和字段配置即可。

| 组件类型 | 默认宽度 `w` | 默认高度 `h` | 布局效果 |
|---------|-------------|-------------|---------|
| **指标卡** (indicator) | 6 | 6 | 占满整行，紧凑醒目 |
| **饼图** (pie) | 3 | 22 | 半行，与其他图表并排 |
| **柱状图** (bar) | 3 | 22 | 半行，与饼图并排 |
| **折线图** (line) | 3 | 22 | 半行，可与其他图表并排 |
| **组合图** (combo) | 6 | 22 | 占满整行 |
| **表格** (table) | 6 | 38 | 占满整行，更高以显示更多数据 |
| **透视表** (pivot) | 6 | 30 | 占满整行 |
| **仪表盘** (gauge) | 2 | 18 | 1/3 行，可三个并排 |

### 推荐布局顺序

```
第1行：指标卡（w=6, h=6）— 占满整行，一目了然
第2行：饼图（w=3）+ 柱状图（w=3）— 并排展示分布和对比
第3行：折线图（w=6）— 占满整行，展示时间趋势（如有日期字段）
第4行：明细表格（w=6, h=38）— 占满整行，展示详细数据
```

> 用户可通过 `chart.w` / `chart.h` 覆盖默认值。

## 常见错误及解决方案

### 错误 1: SelectField 字段显示为空

**原因**: 未加 `_value` 后缀

**解决**: 将 `selectField_xxx` 改为 `selectField_xxx_value`

### 错误 2: DateField 字段报错或显示异常

**原因**: 错误地拆分为年月日时分秒6个字段

**解决**: 直接使用原始 `dateField_xxx`,不要拆分

### 错误 3: 报表创建失败或卡住

**原因**: 字段配置格式错误导致 Schema 构建失败

**解决**:
1. 检查所有 SelectField/RadioField 是否加了 `_value` 后缀
2. 检查 DateField 是否使用了原始 fieldCode
3. 检查 `dataType` 是否与字段类型匹配
4. 检查 `cubeCode` 格式是否正确(使用连字符 `-` 而非下划线 `_`)

### 错误 4: 数值字段无法聚合

**原因**: 字段类型是 `TextField` 而非 `NumberField`

**解决**: 在表单中将字段类型改为 `NumberField`,或使用 `COUNT` 聚合(计数任意类型)

## 获取字段配置信息

### 方法 1: 通过表单 Schema 获取

```bash
openyida get-schema <appType> <formUuid>
```

从返回的 Schema 中提取各字段的 `fieldId` 和类型。

### 方法 2: 查看表单设计器

在宜搭表单设计器中,点击字段查看属性,获取 `fieldCode` 和字段类型。

## 总结

配置报表字段时,牢记以下三点:

1. **SelectField/RadioField/MultiSelectField/CheckboxField**: 必须加 `_value` 后缀
2. **DateField**: 直接使用原始 fieldCode,不要拆分
3. **其他字段**: 直接使用原始 fieldCode

遵循这些规则,可以避免 90% 以上的报表配置错误。
