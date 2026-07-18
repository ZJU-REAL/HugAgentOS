# 宜搭公式函数完整参考

> 来源：[宜搭公式概览](https://docs.aliwork.com/docs/yida_support/wtwabe/cnzrgo/him6xohu4w0gfy7w)
> 宜搭目前共支持 96 种函数，分为 9 大类。

**适用场景说明**：
- ✅ 表单场域：可在表单字段公式中使用
- ✅ 报表场域：可在报表字段公式中使用
- ✅ 数据准备：可在数据准备中使用
- ❌：不支持

---

## 文本函数

| 函数名 | 说明 | 表单 | 报表 | 数据准备 |
|--------|------|------|------|---------|
| `ARRAYGET(array, index)` | 获取数据集中第 index 个数的值（从1开始） | ✅ | ❌ | ❌ |
| `CONCATENATE(text1, text2, ...)` | 将多个文本字符串合并成一个 | ✅ | ❌ | ❌ |
| `LEFT(text, num)` | 从文本左侧返回指定个数的字符；文本长度不足时返回原文本 | ✅ | ✅ | ✅ |
| `RIGHT(text, num)` | 从文本右侧返回指定个数的字符 | ✅ | ✅ | ✅ |
| `LEN(text)` | 获取文本字符串中的字符个数 | ✅ | ❌ | ❌ |
| `LOWER(text)` | 将文本中所有大写字母转换为小写 | ✅ | ✅ | ✅ |
| `UPPER(text)` | 将文本中所有小写字母转换为大写 | ✅ | ✅ | ✅ |
| `MID(text, start, num)` | 返回文本中从 start 位置开始的 num 个字符 | ✅ | ✅ | ✅ |
| `PINYINHEADCHAR(text)` | 返回字符串中每个汉字的首字母（大写） | ✅ | ❌ | ❌ |
| `REPLACE(text, start, num, newText)` | 将文本中从 start 开始的 num 个字符替换为 newText | ✅ | ✅ | ✅ |
| `REPT(text, times)` | 将文本重复指定次数 | ✅ | ❌ | ❌ |
| `RMBFORMAT(number)` | 将数字格式化为人民币格式 | ✅ | ❌ | ❌ |
| `SEARCH(text1, text2)` | 获取 text1 在 text2 中的开始位置 | ✅ | ✅（类似POS） | ✅（类似POS） |
| `SPLIT(text, delimiter)` | 将文本按指定分隔符分割成数组，需配合 ARRAYGET 使用 | ✅ | ❌ | ❌ |
| `TEXT(value, format)` | 将数值格式化为文本 | ✅ | ❌ | ❌ |
| `TRIM(text)` | 删除字符串首尾空格，保留内部词间空格 | ✅ | ❌ | ❌ |
| `UUID()` | 生成唯一字符串，赋值给单行输入框 | ✅ | ❌ | ❌ |
| `VALUE(text)` | 将文本转换成数字 | ✅ | ✅（类似VAL） | ✅（类似VAL） |
| `STRINGTONUMBER(text)` | 将文本类型的数字转换成数值类型 | ❌ | ✅ | ❌ |
| `LEFTTRIM(text)` | 去掉文本左边的空格 | ❌ | ✅ | ✅ |
| `RIGHTTRIM(text)` | 去掉文本右边的空格 | ❌ | ✅ | ✅ |
| `CONCAT(text1, text2, ...)` | 将多个字段连接为一个字符串 | ❌ | ✅ | ✅ |
| `SPLITPART(text, delimiter, n)` | 将字符串按分隔字符分为 N 个子串，返回第 n 个 | ❌ | ✅ | ✅ |
| `NUMBERTOSTRING(number)` | 将数值类型转换成字符串 | ❌ | ✅ | ✅ |

### 文本函数示例

```
# 拼接姓名和部门
CONCATENATE(textField_name, "-", textField_dept)

# 截取编号前4位
LEFT(textField_code, 4)

# 获取字符串长度
LEN(textField_content)

# 生成唯一ID
UUID()

# 格式化金额为人民币
RMBFORMAT(numberField_amount)

# 按逗号分割后取第1个
ARRAYGET(SPLIT(textField_tags, ","), 1)
```

---

## 时间函数

| 函数名 | 说明 | 表单 | 报表 | 数据准备 |
|--------|------|------|------|---------|
| `CASCADEDATEINTERVAL(dateRangeField)` | 计算日期区间组件中开始和结束日期的相隔天数 | ✅ | ❌ | ❌ |
| `CASCADEDATEINTERVALV2(dateRangeField)` | 返回日期区间2个日期的天数 | ✅ | ❌ | ❌ |
| `DATE(timestamp)` | 将时间戳转换为日期对象 | ✅ | ❌ | ❌ |
| `DATEDELTA(dateField, days)` | 将指定日期加/减指定天数 | ✅ | ❌ | ❌ |
| `DAY(dateField)` | 返回某日期的天数（日） | ✅ | ❌ | ❌ |
| `DAYBEGIN(dateField)` | 将日期的时分秒置零，返回当天开始时间戳 | ✅ | ❌ | ❌ |
| `DAYEND(dateField)` | 将日期的时分秒设置为最大，返回当天结束时间戳 | ✅ | ❌ | ❌ |
| `DAYS(dateField1, dateField2)` | 返回两个日期之间的天数 | ✅ | ❌ | ❌ |
| `DAYS360(dateField1, dateField2)` | 按360天/年计算两个日期间相差的天数 | ✅ | ❌ | ❌ |
| `HOUR(dateField)` | 返回某日期的小时数 | ✅ | ❌ | ❌ |
| `ISOWEEKNUM(dateField)` | 返回指定日期在当年的周数（ISO标准） | ✅ | ❌ | ❌ |
| `MINUTE(dateField)` | 返回某日期的分钟数 | ✅ | ❌ | ❌ |
| `MONTH(dateField)` | 返回某日期的月份 | ✅ | ❌ | ❌ |
| `NETWORKDAYS(startDate, endDate)` | 计算两个日期之间的工作日天数（排除周末） | ✅ | ❌ | ❌ |
| `NOW()` | 返回当前时间 | ✅ | ✅ | ✅ |
| `SECOND(dateField)` | 返回某日期的秒数 | ✅ | ❌ | ❌ |
| `SYSTIME()` | 返回当前服务器时间 | ✅ | ❌ | ❌ |
| `TIME(hour, minute, second)` | 返回特定时间的十进制数字 | ✅ | ❌ | ❌ |
| `TIMESTAMP(dateObject)` | 将日期对象转换成时间戳（用于给日期组件赋值） | ✅ | ❌ | ❌ |
| `TODAY()` | 返回今天的日期 | ✅ | ❌ | ❌ |
| `WEEKNUM(dateField)` | 返回特定日期的周数 | ✅ | ✅（类似WEEK） | ✅（类似WEEK） |
| `WORKDAY(dateField, n, holidays)` | 计算距某日期 N 个工作日后的日期，可指定假期 | ✅ | ❌ | ❌ |
| `YEAR(dateField)` | 返回某日期的年份 | ✅ | ❌ | ❌ |
| `YEARBEGIN(dateField)` | 获取日期所在年份的第一天 | ✅ | ❌ | ❌ |
| `YEAREND(dateField)` | 获取日期所在年份的最后一天 | ✅ | ❌ | ❌ |
| `DATEFORMAT(dateField, format)` | 将日期格式化成字符串 | ❌ | ✅ | ✅ |
| `STRINGTODATE(text)` | 将文本类型的日期转换成日期类型 | ❌ | ✅ | ✅ |
| `DATEDIFF(dateField1, dateField2, unit)` | 计算两个时间段字段在指定时间粒度上的差值 | ❌ | ✅ | ✅ |
| `DATEADD(dateField, unit, offset)` | 在日期字段基础上增加指定时间粒度的偏移量 | ❌ | ✅ | ✅ |
| `QUARTER(dateField, isFiscal)` | 计算日期在年份中的季度；isFiscal=1 按财年（4月），0 按自然年 | ❌ | ✅ | ✅ |
| `FROMUNIXTIME(unixtime)` | 将数字型 Unix 时间戳转为日期值 | ❌ | ✅ | ✅ |

### 时间函数示例

```
# 获取今天日期
TODAY()

# 获取当前时间
NOW()

# 计算两个日期相差天数
DAYS(dateField_start, dateField_end)

# 在入职日期基础上加30天（试用期结束）
DATEDELTA(dateField_joinDate, 30)

# 获取日期的年份
YEAR(dateField_birthday)

# 获取日期的月份
MONTH(dateField_birthday)

# 计算工龄（年）
INT(DAYS(TODAY(), dateField_joinDate) / 365)
```

---

## 数组函数

| 函数名 | 说明 | 表单 | 报表 | 数据准备 |
|--------|------|------|------|---------|
| `ArrayToString(array, delimiter)` | 将数组转换为字符串 | ❌ | ✅ | ✅ |
| `StringToArray(text, delimiter)` | 将字符串解析为数组 | ❌ | ✅ | ✅ |
| `ArrayLength(array)` | 返回数组长度 | ❌ | ✅ | ✅ |
| `ArrayCat(array1, array2, ...)` | 返回多数组拼接结果 | ❌ | ✅ | ✅ |

---

## 逻辑函数

| 函数名 | 说明 | 表单 | 报表 | 数据准备 |
|--------|------|------|------|---------|
| `EQ(value1, value2)` | 判断相等，返回 true/false | ✅ | ❌ | ❌ |
| `NE(value1, value2)` | 判断不等，返回 true/false | ✅ | ❌ | ❌ |
| `AND(condition1, condition2, ...)` | 所有条件同时成立时返回 true | ✅ | ❌ | ❌ |
| `OR(condition1, condition2, ...)` | 任一条件成立时返回 true | ✅ | ❌ | ❌ |
| `NOT(condition)` | 对条件取反 | ✅ | ❌ | ❌ |
| `XOR(condition1, condition2)` | 异或：两个条件不同时返回 true | ✅ | ❌ | ❌ |
| `FALSE()` | 返回布尔值 false | ✅ | ❌ | ❌ |
| `TRUE()` | 返回布尔值 true | ✅ | ❌ | ❌ |
| `GE(value1, value2)` | 大于等于，value1 >= value2 返回 true | ✅ | ❌ | ❌ |
| `LE(value1, value2)` | 小于等于，value1 <= value2 返回 true | ✅ | ❌ | ❌ |
| `GT(value1, value2)` | 大于，value1 > value2 返回 true | ✅ | ❌ | ❌ |
| `LT(value1, value2)` | 小于，value1 < value2 返回 true | ✅ | ❌ | ❌ |
| `IF(condition, trueValue, falseValue)` | 条件判断：条件为真返回 trueValue，否则返回 falseValue | ✅ | ❌ | ❌ |
| `ISEMPTY(field)` | 判断字段是否为空 | ✅ | ❌ | ❌ |
| `ISNULL(field)` | 判断明细内组件值是否为空，或多选框值是否为空 | ✅ | ❌ | ❌ |
| `HASEMPTYTEXT(field)` | 判断明细内组件提交的数组值中是否包含空字符串 | ✅ | ❌ | ❌ |
| `TIMECOMPARE(dateField1, dateField2)` | 比较两个日期大小，dateField1 > dateField2 返回 true | ✅ | ❌ | ❌ |
| `NUMBERCOMPARE(numberField1, numberField2)` | 比较两个数字大小，numberField1 > numberField2 返回 true | ✅ | ❌ | ❌ |
| `CASEWHEN(field, case1, result1, ...)` | 类似 SQL 的 CASE WHEN 语法 | ❌ | ✅ | ✅ |

### 逻辑函数示例

```
# 简单条件判断
IF(numberField_score >= 90, "优秀", "良好")

# 嵌套 IF（多级判断）
IF(numberField_score >= 90, "A", IF(numberField_score >= 60, "B", "C"))

# AND 多条件
IF(AND(numberField_age >= 18, numberField_age <= 60), "适龄", "不适龄")

# OR 任一条件
IF(OR(EQ(radioField_status, "已完成"), EQ(radioField_status, "已关闭")), "已结束", "进行中")

# 判断字段是否为空
IF(ISEMPTY(textField_remark), "无备注", textField_remark)
```

---

## 数学函数

| 函数名 | 说明 | 表单 | 报表 | 数据准备 |
|--------|------|------|------|---------|
| `COUNT(field)` | 统计指定表单提交的数据总数 | ✅ | ✅ | ❌ |
| `ADD(number1, number2, ...)` | 计算多个字段值的总和 | ✅ | ❌ | ❌ |
| `AVERAGE(number1, number2, ...)` | 获取一组数值的算术平均值 | ✅ | ✅（AVG） | ❌ |
| `MAX(number1, number2, ...)` | 返回一组数字中的最大值 | ✅ | ✅ | ❌ |
| `MIN(number1, number2, ...)` | 返回一组数字中的最小值 | ✅ | ✅ | ❌ |
| `ABS(number)` | 返回数值的绝对值 | ✅ | ❌ | ❌ |
| `ROUND(number, digits)` | 将数值四舍五入到指定小数位数 | ✅ | ❌ | ✅ |
| `CEILING(number, significance)` | 向上舍入为最接近指定基数的倍数 | ✅ | ❌ | ❌ |
| `FLOOR(number, significance)` | 向下舍入为最接近指定基数的倍数 | ✅ | ❌ | ❌ |
| `INT(number)` | 将数字向下舍入到最接近的整数 | ✅ | ❌ | ❌ |
| `LOG(number, base)` | 根据指定底数返回数字的对数 | ✅ | ❌ | ❌ |
| `MOD(number, divisor)` | 返回两数相除的余数 | ✅ | ❌ | ✅ |
| `POWER(number, power)` | 计算数值的乘幂 | ✅ | ❌ | ❌ |
| `FIXED(number, digits)` | 将数字舍入到指定小数位并以文本返回 | ✅ | ❌ | ❌ |
| `SQRT(number)` | 返回数值的正平方根 | ✅ | ❌ | ❌ |
| `SUM(number1, number2, ...)` | 对所有参数求和 | ✅ | ✅ | ❌ |
| `PRODUCT(number1, number2, ...)` | 返回所有参数的乘积 | ✅ | ❌ | ❌ |
| `SUMPRODUCT(array1, array2)` | 将数组间对应元素相乘后返回乘积之和 | ✅ | ❌ | ❌ |
| `LARGE(array, k)` | 返回数据集中第 k 个最大值 | ✅ | ❌ | ❌ |
| `SMALL(array, k)` | 返回数据集中第 k 个最小值 | ✅ | ❌ | ❌ |
| `COUNTDISTINCT(field)` | 对字段进行去重计数 | ❌ | ✅ | ❌ |
| `ParseDouble(value)` | 将字符串或整数强转为浮点数 | ❌ | ❌ | ✅ |
| `ParseInt(value)` | 将字符串或浮点数强转为整数 | ❌ | ❌ | ✅ |

### 数学函数示例

```
# 子表金额求和
SUM(tableField_items.numberField_amount)

# 计算平均分
AVERAGE(numberField_score1, numberField_score2, numberField_score3)

# 四舍五入保留2位小数
ROUND(numberField_price * numberField_qty, 2)

# 取整
INT(numberField_total / numberField_count)

# 计算折扣后金额
PRODUCT(numberField_price, numberField_discount)

# 子表数量 × 单价求和（SUMPRODUCT）
SUMPRODUCT(tableField_items.numberField_qty, tableField_items.numberField_price)
```

---

## 集合函数

| 函数名 | 说明 | 表单 | 报表 | 数据准备 |
|--------|------|------|------|---------|
| `DIFFERENCESET(set1, set2)` | 计算两个集合的差集（set1 中有但 set2 中没有的元素） | ✅ | ❌ | ❌ |
| `INTERSECTIONSET(set1, set2)` | 计算两个集合的交集 | ✅ | ❌ | ❌ |
| `SUBSET(set1, set2)` | 判断 set2 是否是 set1 的子集，返回 true/false | ✅ | ❌ | ❌ |
| `UNIONSET(set1, set2)` | 计算两个集合的并集 | ✅ | ❌ | ❌ |

---

## 校验函数

| 函数名 | 说明 | 表单 | 报表 | 数据准备 |
|--------|------|------|------|---------|
| `ARRAYREPEATED(array)` | 判断数组中是否有重复元素，有则返回 true | ✅ | ❌ | ❌ |
| `EXIST(value, set)` | 判断 value 是否在集合 set 中存在 | ✅ | ❌ | ❌ |
| `EXACT(text1, text2)` | 比较两个字符串是否完全相同（区分大小写） | ✅ | ❌ | ❌ |

---

## 人员函数

| 函数名 | 说明 | 表单 | 报表 | 数据准备 |
|--------|------|------|------|---------|
| `USER()` | 无参：获取当前登录人；有参 `USER(level)`：获取当前登录人的第 level 级主管 | ✅ | ❌ | ❌ |
| `USERFIELD(employeeField, attr)` | 人员搜索框联动带出基本信息 | ✅ | ❌ | ❌ |
| `DIRECTOR()` | 获取当前登录人的主管名称 | ✅ | ❌ | ❌ |
| `EMPLOYEE(userId)` | 返回对应人员信息对象数组（人员组件底层的值） | ✅ | ❌ | ❌ |
| `GETUSERNAME()` | 获取当前登录人昵称 | ✅ | ❌ | ❌ |
| `LOGINUSER()` | 获取当前登录人信息 | ✅ | ❌ | ❌ |
| `LOGINUSERWORKNO()` | 获取当前登录人员工唯一 ID（UserID） | ✅ | ❌ | ❌ |
| `DEPTNAME(userId)` | 根据 userId 获取指定用户所在的部门信息 | ✅ | ❌ | ❌ |

### 人员函数示例

```
# 自动填充当前登录人（赋值给成员字段）
USER()

# 获取当前登录人昵称（赋值给文本字段）
GETUSERNAME()

# 获取当前登录人的直属主管
USER(1)

# 获取当前登录人的部门名称
DEPTNAME(LOGINUSERWORKNO())
```

---

## 高级公式函数

> ⚠️ 高级函数用于跨表数据操作（业务关联规则），有严格的用量限制。

| 函数名 | 说明 | 表单 | 报表 | 数据准备 |
|--------|------|------|------|---------|
| `INSERT(目标表, 目标列1, 目标值1, ...)` | 将当前表数据插入到目标表 | ✅ | ❌ | ❌ |
| `UPDATE(目标表, 主条件, 子条件, 目标列1, 目标值1, ...)` | 更新目标表中符合条件的数据 | ✅ | ❌ | ❌ |
| `UPSERT(目标表, 主条件, 子条件, 目标列1, 目标值1, ...)` | 往目标表插入或更新数据（有则更新，无则插入） | ✅ | ❌ | ❌ |
| `DELETE(目标表, 主条件, 子条件)` | 删除目标表中符合条件的数据 | ✅ | ❌ | ❌ |

### 高级函数配置要点

- 高级函数只能配置在「表单设置 → 表单事件 → 公式执行 → 添加业务关联规则」中，不是普通字段默认值公式。
- 公式编辑器中目标表、目标列、当前表字段必须通过左侧列表点击插入令牌；不要直接手敲表单名或字段名。
- `子条件` 用于定位目标表某条数据下的子表单明细；不操作子表单时必须写 `""`，不能省略。
- 在 `EQ()` 等逻辑函数中，除 `AND()`、`OR()` 外，第一个参数必须是目标表中的组件，不能与当前表字段写反。

### 高级函数用量限制

| 函数 | 目标表类型 | 用量限制 |
|------|-----------|---------|
| `INSERT` | 主表 | 批量导入无条数限制（建议 ≤1000 条） |
| `INSERT` | 子表单插入主表 | 免费版 ≤50 条；轻享/专业/专属版 ≤500 条 |
| `UPDATE` | 主表 | 单次最多更新 100 条，超出报错 |
| `UPDATE` | 子表单 | 免费版 ≤50 条；轻享/专业/专属版 ≤500 条 |
| `UPSERT` | 主表 | 单次最多 100 条，超出报错 |
| `UPSERT` | 子表单 | 免费版 ≤50 条；轻享/专业/专属版 ≤500 条 |
| `DELETE` | 主表 | 单次最多删除 100 条 |
| `DELETE` | 子表单 | 轻享/专业/专属版 ≤500 条 |
