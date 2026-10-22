# yida-integration 使用示例

## 示例 1：表单新增时发送钉钉通知

### 场景

当"请假申请"表单有新记录提交时，自动发送钉钉消息通知给 HR（userId: `0162193625672514`）。

### 前置步骤：获取字段 ID

```bash
openyida get-schema APP_XXX FORM-LEAVE
# 从 stdout 中提取字段 ID，并用结构化文件写入工具更新 <projectRoot>/.cache/<项目名>-schema.json：
# 申请人：employeeField_applicant
# 请假类型：selectField_leaveType
# 开始日期：dateField_startDate
# 结束日期：dateField_endDate
```

### 执行命令

```bash
openyida integration create APP_XXX FORM-LEAVE "请假申请通知" \
  --receivers 0162193625672514 \
  --title "新请假申请：#{employeeField_applicant-EmployeeField}# 提交了#{selectField_leaveType-SelectField}#申请" \
  --content "请假时间：#{dateField_startDate-DateField}# 至 #{dateField_endDate-DateField}#，请及时处理。" \
  --events insert \
  --publish
```

### 输出

```json
{
  "success": true,
  "published": true,
  "processCode": "LPROC-A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q7R8S9T0",
  "flowName": "请假申请通知",
  "appType": "APP_XXX",
  "formUuid": "FORM-LEAVE",
  "formEventTypes": ["insert"]
}
```

---

## 示例 2：跨表数据同步（新增数据节点）

### 场景

当"销售订单"表单新增记录时，自动将订单数据同步到"销售统计"表单，并通知销售主管。

### 执行命令

```bash
openyida integration create APP_XXX FORM-ORDER "订单新增同步通知" \
  --receivers manager_user_id \
  --title "新订单：#{textField_orderNo-TextField}#" \
  --content "客户：#{textField_customer-TextField}#，金额：#{numberField_amount-NumberField}#" \
  --events insert \
  --add-data-form-uuid FORM-STATS \
  --add-data-assignment "textField_orderNo:processVar:textField_orderNo" \
  --add-data-assignment "textField_customer:processVar:textField_customer" \
  --add-data-assignment "numberField_amount:processVar:numberField_amount" \
  --add-data-assignment "dateField_syncDate:literal:$(date +%s000)" \
  --publish
```

### 输出

```json
{
  "success": true,
  "published": true,
  "processCode": "LPROC-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
  "flowName": "订单新增同步通知"
}
```

---

## 示例 3：带跨表查询的通知（获取单条数据节点）

### 场景

当"工单处理"表单更新时，从"客户信息"表单查询对应客户的联系人，再发送通知。

### 执行命令

```bash
openyida integration create APP_XXX FORM-TICKET "工单处理通知" \
  --receivers service_user_id \
  --title "工单已更新：#{textField_ticketNo-TextField}#" \
  --content "处理状态：#{selectField_status-SelectField}#" \
  --events update \
  --data-form-uuid FORM-CUSTOMER \
  --data-condition "textField_customerId:客户ID:textField_customerId:TextField" \
  --publish
```

---

## 变量引用格式对照

| 使用场景 | 格式 | 示例 |
|---------|------|------|
| 通知标题/内容中引用字段 | `#{fieldId-ComponentType}#` | `#{textField_name-TextField}#` |
| 赋值中引用触发表单字段 | `processVar` + `fieldId` | `--add-data-assignment "targetField:processVar:sourceField"` |
| 赋值固定值 | `literal` + 值 | `--add-data-assignment "statusField:literal:已处理"` |
| 赋值公式 | `column` + 公式 | `--add-data-assignment "totalField:column:CONCATENATE(#{field1},#{field2})"` |

## 常见错误

| 错误 | 原因 | 解决方式 |
|------|------|---------|
| `fieldId 不存在` | 手写猜测了 fieldId | 先执行 `openyida get-schema` 获取真实 fieldId |
| `TIANSHU_000030` | CSRF 校验失败 | 脚本自动重试，无需手动干预 |
| `307` | 登录过期 | 脚本自动重新登录，无需手动干预 |
| 消息未发送 | receivers 填写了姓名而非 userId | 填写宜搭/钉钉用户 ID（userId），不是姓名 |
