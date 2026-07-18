# yida-formula 使用示例

## 示例 1：创建带公式的报销金额计算字段

### 场景

报销申请表中，需要自动计算"总金额 = 交通费 + 住宿费 + 餐饮费"，并保留 2 位小数。

### 前置步骤：获取字段 ID

```bash
openyida get-schema APP_XXX FORM-YYY
# 从 stdout 中找到各字段的 fieldId，并用结构化文件写入工具更新 <projectRoot>/.cache/<项目名>-schema.json：
# 交通费：numberField_transport
# 住宿费：numberField_hotel
# 餐饮费：numberField_meal
# 总金额：numberField_total（待配置公式）
```

### 执行：update 模式配置公式

使用 create_file / Write / file edit tool 创建 `<projectRoot>/.cache/openyida/expense/formula-config.json`：

```json
[
  {
    "action": "update",
    "label": "总金额",
    "changes": {
      "behavior": "READONLY",
      "valueType": "formula",
      "complexValue": {
        "complexType": "formula",
        "formula": "ROUND(#{numberField_transport} + #{numberField_hotel} + #{numberField_meal}, 2)"
      },
      "formula": "ROUND(#{numberField_transport} + #{numberField_hotel} + #{numberField_meal}, 2)"
    }
  }
]
```

```bash
openyida create-form update APP_XXX FORM-YYY .cache/openyida/expense/formula-config.json
```

### 输出

```json
{
  "success": true,
  "formUuid": "FORM-YYY",
  "updatedFields": ["numberField_total"]
}
```

---

## 示例 2：IF 条件判断 + 多级嵌套

### 场景

绩效评分表中，根据分数自动计算等级：≥90 → A，≥75 → B，≥60 → C，否则 → D。

### 公式

```
IF(GE(#{numberField_score}, 90), "A", IF(GE(#{numberField_score}, 75), "B", IF(GE(#{numberField_score}, 60), "C", "D")))
```

### 字段配置

```json
{
  "action": "update",
  "label": "绩效等级",
  "changes": {
    "behavior": "READONLY",
    "valueType": "formula",
    "complexValue": {
      "complexType": "formula",
      "formula": "IF(GE(#{numberField_score}, 90), \"A\", IF(GE(#{numberField_score}, 75), \"B\", IF(GE(#{numberField_score}, 60), \"C\", \"D\")))"
    },
    "formula": "IF(GE(#{numberField_score}, 90), \"A\", IF(GE(#{numberField_score}, 75), \"B\", IF(GE(#{numberField_score}, 60), \"C\", \"D\")))"
  }
}
```

---

## 示例 3：子表金额求和

### 场景

采购申请表中，子表（明细列表）包含多行商品，需要自动汇总子表中所有行的"小计"字段。

### 公式

```
SUM(#{tableField_items.numberField_subtotal})
```

### 注意事项

- 子表字段引用格式：`#{子表fieldId.子表内字段fieldId}`
- 公式中的 fieldId 必须通过 `openyida get-schema` 获取，不能猜测
- 比较运算必须用函数（`GE`、`LE`、`GT`、`LT`、`EQ`），禁止使用 `>=`、`<=` 等符号

---

## 示例 4：自动填充当前登录人和部门

### 场景

申请表提交时，自动填充"申请人"（成员字段）和"所在部门"（文本字段）。

### 公式

```
# 申请人字段（EmployeeField）
USER()

# 所在部门字段（TextField）
DEPTNAME(LOGINUSERWORKNO())
```

### 字段配置

```json
[
  {
    "action": "update",
    "label": "申请人",
    "changes": {
      "valueType": "formula",
      "complexValue": { "complexType": "formula", "formula": "USER()" },
      "formula": "USER()"
    }
  },
  {
    "action": "update",
    "label": "所在部门",
    "changes": {
      "valueType": "formula",
      "complexValue": { "complexType": "formula", "formula": "DEPTNAME(LOGINUSERWORKNO())" },
      "formula": "DEPTNAME(LOGINUSERWORKNO())"
    }
  }
]
```
