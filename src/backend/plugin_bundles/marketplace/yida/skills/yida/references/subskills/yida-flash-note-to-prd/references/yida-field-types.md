# 宜搭字段类型参考

> 本文档列出宜搭平台支持的标准字段类型，供 PRD 生成时的字段设计使用。
> 字段类型必须使用以下标准类型，不得使用自定义或不存在的类型。

| 宜搭字段类型 | 适用场景 |
|-------------|---------|
| TextField | 短文本（姓名、标题等） |
| TextareaField | 长文本（描述、备注等） |
| NumberField | 数字（金额、数量、积分等） |
| SelectField | 单选下拉（状态、类别等），需列出选项值 |
| MultiSelectField | 多选（标签、多选类别等） |
| DateField | 日期（提交时间、截止日期等） |
| EmployeeField | 人员选择（提交人、审批人等） |
| DepartmentSelectField | 部门选择 |
| AttachmentField | 附件上传（文件、图片等） |
| ImageField | 图片上传 |
| RadioField | 单选按钮（是/否、性别等少量选项） |
| CheckboxField | 复选框 |
| AddressField | 地址 |
| TableField | 子表单/明细（订单明细、费用明细等） |
| AssociationFormField | 稳定关联其他表单记录 |
| SerialNumberField | 流水号/业务编号 |
