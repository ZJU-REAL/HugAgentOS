
# 宜搭表单公式编写规范

## 触发条件

**正向触发**（以下场景必须使用本技能）：
- 用户说"配置公式"、"计算字段"、"自动计算"、"字段联动"
- 用户说"求和"、"平均值"、"条件判断"、"IF 公式"
- 用户需要在表单字段上配置默认值公式、计算公式
- 用户需要配置字段的自定义校验规则
- 用户需要了解宜搭公式函数的用法和语法

**与相邻技能的边界**：
| 场景 | 使用技能 |
|------|---------|
| 字段值自动计算（如：总金额 = 单价 × 数量） | **本技能** |
| 字段默认值（如：默认填充当前登录人） | **本技能** |
| 字段校验规则（如：结束日期必须晚于开始日期） | **本技能** |
| 用户明确要求业务关联规则或 UPSERT 等高级函数 | `yida-business-rule` |
| 提交后更新其他表单数据（常规/推荐方案） | `yida-integration` |
| 创建 NumberField、TextField 等字段 | `yida-create-form-page` |


## 概述

宜搭公式可以：
1. **自动计算字段默认值**：在数值、单行文本、日期、成员等字段上配置公式，根据其他字段自动计算出值
2. **配置校验规则**：在提交时校验数据是否符合规则（在表单属性的「校验」中配置）
3. **辅助识别业务关联规则边界**：提交后增删改其他表单数据的高级函数已拆分到 `yida-business-rule`

> 完整函数列表参见 `../../references/formula-functions.md`

官方示例中心的公式类示例基本落在普通表单或流程表单 schema 中，而不是自定义页面。遇到“日期转换、流水号、登录人、主管、相隔时间、求和”等能力时，默认先配置字段公式或字段联动；只有公式无法表达、且需要跨表提交后动作时，再切到 `yida-integration` 或 `yida-business-rule`。

---

## ⚠️ 重要：公式配置方式说明

**宜搭没有独立的"公式字段"组件**。公式是配置在普通字段（`NumberField`、`TextField`、`DateField`、`EmployeeField` 等）的属性上的：

| 配置位置 | 说明 | 典型场景 |
|---------|------|---------|
| 字段的「默认值」→「公式」 | 字段打开时自动计算并填入值 | 总金额 = 单价 × 数量 |
| 字段的「校验」→「自定义校验」 | 提交时校验字段值是否合法 | 结束日期必须晚于开始日期 |
| 表单设置 → 表单事件 → 公式执行 → 添加业务关联规则 | 提交后触发跨表数据操作 | 改用 `yida-business-rule` |

---

## 公式字段的 Schema 结构

通过 `openyida create-form` 的 create 或 update 模式，可以直接在字段上配置公式。

### 字段引用格式

公式中引用其他字段时，使用 **`#{fieldId}`** 格式（不是直接写 fieldId）：

```
# ✅ 正确：用 #{} 包裹 fieldId
ROUND(#{numberField_abc} * #{numberField_xyz}, 2)

# ❌ 错误：直接写 fieldId
ROUND(numberField_abc * numberField_xyz, 2)
```

### 字段 Schema 中的公式属性

需要同时设置以下三个属性：

| 属性 | 值 | 说明 |
|------|-----|------|
| `valueType` | `"formula"` | 声明该字段值由公式计算 |
| `complexValue` | `{"complexType":"formula","formula":"<公式字符串>"}` | 公式配置对象 |
| `formula` | `"<公式字符串>"` | 与 complexValue.formula 相同的公式字符串 |

### create 模式：创建带公式的字段

```json
[
  {
    "type": "NumberField",
    "label": "总金额",
    "behavior": "READONLY",
    "valueType": "formula",
    "complexValue": {
      "complexType": "formula",
      "formula": "ROUND(#{numberField_price} * #{numberField_qty}, 2)"
    },
    "formula": "ROUND(#{numberField_price} * #{numberField_qty}, 2)"
  }
]
```

### update 模式：给已有字段配置公式

```json
[
  {
    "action": "update",
    "label": "总金额",
    "changes": {
      "valueType": "formula",
      "complexValue": {
        "complexType": "formula",
        "formula": "ROUND(#{numberField_price} * #{numberField_qty}, 2)"
      },
      "formula": "ROUND(#{numberField_price} * #{numberField_qty}, 2)"
    }
  }
]
```

### ⚠️ 重要：公式中的 fieldId 必须是真实 ID

公式中引用的 `fieldId`（如 `numberField_abc123`）必须是表单中真实存在的字段 ID，不能猜测。**正确工作流程**：

```
Step 1: create 模式创建基础字段（不含公式）
         ↓
Step 2: openyida get-schema 获取各字段的真实 fieldId
         ↓
Step 3: create 或 update 模式添加/更新带公式的字段，引用真实 #{fieldId}
```

---

## 公式语法规则

### 基本规则

1. **函数名必须全大写**：`SUM`、`IF`、`CONCATENATE`，不能写成 `sum`、`if`
2. **所有符号必须是英文**：括号 `()`、逗号 `,`、引号 `""`，不能用中文符号
3. **字符串常量用双引号**：`"优秀"`、`"已完成"`
4. **支持基本算术运算符**：`+`（加）、`-`（减）、`*`（乘）、`/`（除）
5. **比较运算必须用函数，不能用符号**：禁止使用 `>=`、`<=`、`>`、`<`、`==`、`!=`，必须改用 `GE()`、`LE()`、`GT()`、`LT()`、`EQ()`、`NE()` 函数
6. **隐藏字段需开启「始终提交」**：否则隐藏字段不参与公式计算

### 字段引用规则

公式中引用表单字段时，使用 **`#{fieldId}`** 格式（必须用 `#{}` 包裹）：

```
# 引用主表字段（用 #{} 包裹 fieldId）
#{textField_abc123}

# 引用子表（TableField）内的字段
#{tableField_xxx.numberField_yyy}

# 引用当前登录用户（无需字段 ID）
USER()

# 引用当前时间（无需字段 ID）
NOW()
TODAY()
```

> **如何获取 fieldId**：使用 `openyida get-schema <appType> <formUuid>` 获取表单 Schema，其中每个字段的 `fieldId` 属性即为字段 ID。

### 赋值类型限制

公式结果只能赋值给**同类型**的组件：

| 公式/来源类型 | 可赋值的目标组件 |
|-------------|----------------|
| 文本函数结果 | 单行文本、多行文本 |
| 数值计算结果 | 数值、单行文本、多行文本 |
| 日期函数结果 | 日期 |
| 人员函数结果 | 成员 |
| 单选/下拉单选 | 单选、下拉单选 |
| 复选/下拉复选 | 复选、下拉复选 |

---

## 业务关联规则边界

业务关联规则、高级函数、`INSERT`/`UPDATE`/`DELETE`/`UPSERT` 已拆分到独立子技能 `yida-business-rule`。本技能只处理普通字段公式、默认值公式和自定义校验公式。

---

## 函数分类速查

> 完整函数参数说明见 `../../references/formula-functions.md`

### 文本函数（表单场域）

| 函数 | 用途 | 示例 |
|------|------|------|
| `CONCATENATE(text1, text2, ...)` | 拼接多个文本 | `CONCATENATE(#{textField_firstName}, #{textField_lastName})` |
| `LEFT(text, n)` | 取左侧 n 个字符 | `LEFT(#{textField_code}, 4)` |
| `RIGHT(text, n)` | 取右侧 n 个字符 | `RIGHT(#{textField_idCard}, 4)` |
| `MID(text, start, n)` | 取中间字符 | `MID(#{textField_code}, 3, 4)` |
| `LEN(text)` | 获取字符串长度 | `LEN(#{textField_content})` |
| `TRIM(text)` | 去除首尾空格 | `TRIM(#{textField_name})` |
| `UPPER(text)` | 转大写 | `UPPER(#{textField_code})` |
| `LOWER(text)` | 转小写 | `LOWER(#{textField_email})` |
| `REPLACE(text, start, n, newText)` | 替换指定位置字符 | `REPLACE(#{textField_phone}, 4, 4, "****")` |
| `TEXT(value, format)` | 数值格式化为文本 | `TEXT(#{numberField_amount}, "0.00")` |
| `VALUE(text)` | 文本转数字 | `VALUE(#{textField_numStr})` |
| `UUID()` | 生成唯一字符串 | `UUID()` |
| `RMBFORMAT(number)` | 格式化为人民币 | `RMBFORMAT(#{numberField_amount})` |
| `SPLIT(text, delimiter)` | 按分隔符分割为数组 | `SPLIT(#{textField_tags}, ",")` |
| `ARRAYGET(array, index)` | 取数组第 n 个元素 | `ARRAYGET(SPLIT(#{textField_tags}, ","), 1)` |

### 时间函数（表单场域）

| 函数 | 用途 | 示例 |
|------|------|------|
| `TODAY()` | 今天日期 | `TODAY()` |
| `NOW()` | 当前时间 | `NOW()` |
| `YEAR(date)` | 取年份 | `YEAR(#{dateField_birthday})` |
| `MONTH(date)` | 取月份 | `MONTH(#{dateField_birthday})` |
| `DAY(date)` | 取天数 | `DAY(#{dateField_birthday})` |
| `DAYS(date1, date2)` | 两日期相差天数 | `DAYS(#{dateField_end}, #{dateField_start})` |
| `DATEDELTA(date, n)` | 日期加减 n 天 | `DATEDELTA(#{dateField_start}, 30)` |
| `NETWORKDAYS(start, end)` | 计算工作日天数 | `NETWORKDAYS(#{dateField_start}, #{dateField_end})` |
| `TIMESTAMP(dateObj)` | 日期对象转时间戳 | `TIMESTAMP(TODAY())` |
| `DATE(timestamp)` | 时间戳转日期对象 | `DATE(#{numberField_ts})` |
| `CASCADEDATEINTERVAL(field)` | 日期区间天数 | `CASCADEDATEINTERVAL(#{cascadeDateField_range})` |

### 逻辑函数（表单场域）

| 函数 | 用途 | 示例 |
|------|------|------|
| `IF(cond, trueVal, falseVal)` | 条件判断 | `IF(GE(#{numberField_score}, 90), "优秀", "良好")` |
| `AND(cond1, cond2, ...)` | 多条件同时成立 | `AND(GE(#{numberField_age}, 18), LE(#{numberField_age}, 60))` |
| `OR(cond1, cond2, ...)` | 任一条件成立 | `OR(EQ(#{radioField_status}, "完成"), EQ(#{radioField_status}, "关闭"))` |
| `NOT(cond)` | 条件取反 | `NOT(ISEMPTY(#{textField_name}))` |
| `EQ(val1, val2)` | 判断相等 | `EQ(#{radioField_type}, "A类")` |
| `NE(val1, val2)` | 判断不等 | `NE(#{radioField_status}, "已取消")` |
| `GT(val1, val2)` | 大于 | `GT(#{numberField_amount}, 10000)` |
| `GE(val1, val2)` | 大于等于 | `GE(#{numberField_score}, 60)` |
| `LT(val1, val2)` | 小于 | `LT(#{numberField_stock}, 10)` |
| `LE(val1, val2)` | 小于等于 | `LE(#{numberField_age}, 18)` |
| `ISEMPTY(field)` | 判断字段是否为空 | `ISEMPTY(#{textField_remark})` |
| `TIMECOMPARE(date1, date2)` | 比较日期大小 | `TIMECOMPARE(#{dateField_end}, #{dateField_start})` |

### 数学函数（表单场域）

| 函数 | 用途 | 示例 |
|------|------|------|
| `SUM(n1, n2, ...)` | 求和 | `SUM(#{tableField_items.numberField_amount})` |
| `AVERAGE(n1, n2, ...)` | 平均值 | `AVERAGE(#{numberField_q1}, #{numberField_q2}, #{numberField_q3})` |
| `MAX(n1, n2, ...)` | 最大值 | `MAX(#{numberField_a}, #{numberField_b})` |
| `MIN(n1, n2, ...)` | 最小值 | `MIN(#{numberField_a}, #{numberField_b})` |
| `ROUND(n, digits)` | 四舍五入 | `ROUND(#{numberField_price} * #{numberField_qty}, 2)` |
| `INT(n)` | 向下取整 | `INT(#{numberField_total} / 30)` |
| `ABS(n)` | 绝对值 | `ABS(#{numberField_diff})` |
| `MOD(n, divisor)` | 取余 | `MOD(#{numberField_count}, 7)` |
| `PRODUCT(n1, n2, ...)` | 乘积 | `PRODUCT(#{numberField_price}, #{numberField_qty})` |
| `SUMPRODUCT(arr1, arr2)` | 数组对应元素相乘后求和 | `SUMPRODUCT(#{tableField_items.numberField_qty}, #{tableField_items.numberField_price})` |
| `SQRT(n)` | 平方根 | `SQRT(#{numberField_area})` |
| `POWER(n, p)` | 乘幂 | `POWER(#{numberField_base}, 2)` |
| `COUNT(field)` | 统计数据总数 | `COUNT(#{tableField_items})` |

### 人员函数（表单场域）

| 函数 | 用途 | 示例 |
|------|------|------|
| `USER()` | 当前登录人（赋值给成员字段） | `USER()` |
| `USER(level)` | 当前登录人的第 level 级主管 | `USER(1)` |
| `GETUSERNAME()` | 当前登录人昵称（赋值给文本字段） | `GETUSERNAME()` |
| `LOGINUSERWORKNO()` | 当前登录人员工 ID | `LOGINUSERWORKNO()` |
| `DEPTNAME(userId)` | 指定用户所在部门名称 | `DEPTNAME(LOGINUSERWORKNO())` |
| `DIRECTOR()` | 当前登录人的主管名称 | `DIRECTOR()` |

### 集合函数（表单场域）

| 函数 | 用途 | 示例 |
|------|------|------|
| `UNIONSET(set1, set2)` | 两集合并集 | `UNIONSET(#{checkboxField_a}, #{checkboxField_b})` |
| `INTERSECTIONSET(set1, set2)` | 两集合交集 | `INTERSECTIONSET(#{checkboxField_a}, #{checkboxField_b})` |
| `DIFFERENCESET(set1, set2)` | 两集合差集 | `DIFFERENCESET(#{checkboxField_all}, #{checkboxField_selected})` |
| `EXIST(val, set)` | 判断值是否在集合中 | `EXIST(#{textField_tag}, #{checkboxField_tags})` |

### 校验函数（表单场域）

| 函数 | 用途 | 示例 |
|------|------|------|
| `EXACT(text1, text2)` | 严格比较字符串（区分大小写） | `EXACT(#{textField_password}, #{textField_confirm})` |
| `ARRAYREPEATED(array)` | 判断数组是否有重复 | `ARRAYREPEATED(#{tableField_items.textField_code})` |

---

## 常见场景示例

### 场景 1：子表金额求和

```
SUM(#{tableField_items.numberField_amount})
```

### 场景 2：单价 × 数量计算总价（保留2位小数）

```
ROUND(#{numberField_price} * #{numberField_qty}, 2)
```

### 场景 3：子表多列乘积求和（SUMPRODUCT）

```
SUMPRODUCT(#{tableField_items.numberField_qty}, #{tableField_items.numberField_price})
```

### 场景 4：IF 条件判断等级

```
IF(GE(#{numberField_score}, 90), "A", IF(GE(#{numberField_score}, 75), "B", IF(GE(#{numberField_score}, 60), "C", "D")))
```

### 场景 5：多条件 AND 判断

```
IF(AND(GE(#{numberField_age}, 18), LE(#{numberField_age}, 60)), "适龄员工", "不符合条件")
```

### 场景 6：计算两个日期相差天数

```
DAYS(#{dateField_endDate}, #{dateField_startDate})
```

### 场景 7：在入职日期基础上加 90 天（试用期结束日）

```
DATEDELTA(#{dateField_joinDate}, 90)
```

### 场景 8：计算工龄（年，向下取整）

```
INT(DAYS(TODAY(), #{dateField_joinDate}) / 365)
```

### 场景 9：自动填充当前登录人（成员字段）

```
USER()
```

### 场景 10：自动填充当前登录人昵称（文本字段）

```
GETUSERNAME()
```

### 场景 11：自动填充当前登录人部门

```
DEPTNAME(LOGINUSERWORKNO())
```

### 场景 12：拼接姓名和工号

```
CONCATENATE(#{textField_name}, "(", #{textField_workNo}, ")")
```

### 场景 13：手机号中间4位打码

```
REPLACE(#{textField_phone}, 4, 4, "****")
```

### 场景 14：判断字段是否为空，为空时显示默认值

```
IF(ISEMPTY(#{textField_remark}), "无备注", #{textField_remark})
```

### 场景 15：生成唯一 ID

```
UUID()
```

### 场景 16：计算两个日期区间的工作日天数

```
NETWORKDAYS(#{dateField_startDate}, #{dateField_endDate})
```

### 场景 17：格式化金额为人民币大写

```
RMBFORMAT(#{numberField_amount})
```

### 场景 18：按逗号分割标签字符串，取第一个标签

```
ARRAYGET(SPLIT(#{textField_tags}, ","), 1)
```

---

## 注意事项

### ⚠️ 循环依赖

公式中不能引用当前正在配置公式的字段本身，否则会报「表单中的公式存在循环依赖」错误。

**错误示例**：在 `numberField_total` 的公式中引用 `numberField_total` 自身。

### ⚠️ 隐藏字段参与计算

隐藏字段（`behavior: "HIDDEN"`）默认不参与公式计算。若需要隐藏字段参与计算，必须在字段配置中开启「始终提交」选项。

### ⚠️ 日期字段不建议作为判断条件

日期字段的底层值是时间戳，直接用时间戳做判断条件不准确。推荐做法：
1. 用一个单行文本字段通过公式接收日期值
2. 用该单行文本字段作为判断条件

### ⚠️ 业务关联规则单独处理

`INSERT`/`UPDATE`/`DELETE`/`UPSERT` 属于表单事件里的业务关联规则高级函数，不属于普通字段公式。遇到这类需求时切换到 `yida-business-rule`。

---

## 与其他技能的配合

| 场景 | 配合技能 |
|------|---------|
| 创建表单基础字段结构 | `yida-create-form-page`（创建 `NumberField`、`TextField` 等字段） |
| 获取字段真实 fieldId 用于公式引用 | `yida-get-schema`（get-schema 后从 fieldId 属性获取，再用 `#{fieldId}` 格式写入公式） |
| 配置业务关联规则高级函数 | `yida-business-rule` |
| 配置提交后的常规数据联动 | `yida-integration` |
| 完整函数参数说明 | `../../references/formula-functions.md` |

## 异常处理

| 异常场景 | 处理方式 |
|---------|----------|
| 公式报"函数不存在" | 只能使用 `formula-functions.md` 中列出的函数，检查函数名拼写和大小写 |
| 公式报"循环依赖" | 公式中不能引用当前字段本身，检查是否存在循环引用 |
| 字段值未自动计算 | 确认字段已设置 `valueType: "formula"`，隐藏字段需开启「始终提交」 |
| 比较运算符报错 | 禁止使用 `>=`、`<=` 等符号，必须改用 `GE()`、`LE()` 等函数 |
| 误把高级函数写到字段公式里 | 切换到 `yida-business-rule`，在表单事件的业务关联规则中配置 |
| 字段 ID 引用错误 | 先用 `openyida get-schema` 获取真实 fieldId，公式中用 `#{fieldId}` 格式引用 |

## Memory 策略

本技能不读写 memory。公式配置通过 `openyida create-form` 命令写入宜搭平台，fieldId 等技术信息写入 `.cache/` 临时文件，不依赖跨会话的 memory 状态。
