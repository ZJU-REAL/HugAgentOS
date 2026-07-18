# yida-table-form 使用示例

## 示例 1：批量录入员工信息

### 场景

HR 需要批量录入新员工信息到宜搭表单，支持手动逐行填写和 Excel 粘贴导入。

### 前置步骤：获取表单字段 ID

```bash
openyida get-schema APP_XXX FORM-EMPLOYEE
# 从 stdout 中提取字段 ID，并用结构化文件写入工具更新 <projectRoot>/.cache/<项目名>-schema.json：
# 姓名：textField_name
# 部门：selectField_dept
# 入职日期：dateField_joinDate
# 工号：textField_workNo
```

### 配置区（修改 COLUMNS 适配实际表单）

```javascript
var FORM_UUID = 'FORM-EMPLOYEE';
var DRAFT_KEY = 'yida_table_form_draft_' + FORM_UUID;

var COLUMNS = [
  { label: '姓名',   field: 'textField_name',     type: 'text',   required: true },
  { label: '部门',   field: 'selectField_dept',   type: 'select', required: true,
    options: ['研发部', '产品部', '运营部', '市场部', 'HR部'] },
  { label: '入职日期', field: 'dateField_joinDate', type: 'date',   required: true },
  { label: '工号',   field: 'textField_workNo',   type: 'text',   required: false },
];
```

### 执行步骤

```bash
# Step 1：创建自定义页面
openyida create-page APP_XXX "员工批量录入"
# 输出：formUuid = FORM-BATCHINPUT

# Step 2：编写表格表单代码（参考 yida-table-form 主技能的完整示例代码）
# 输出到 project/pages/src/employee-batch.js

# Step 3：发布页面
openyida publish project/pages/src/employee-batch.js APP_XXX FORM-BATCHINPUT
```

### 输出

```json
{
  "success": true,
  "pageUrl": "https://www.aliwork.com/APP_XXX/custom/FORM-BATCHINPUT"
}
```

---

## 示例 2：Excel 粘贴导入操作流程

### 操作步骤

1. 在 Excel 中选中数据区域（不含表头），按 `Ctrl+C` 复制
2. 在宜搭表格表单页面点击「📋 粘贴 Excel 数据」按钮
3. 系统自动按 Tab 分隔解析列，按换行分隔解析行
4. 数据追加到现有行后，可手动修正错误
5. 点击「✓ 提交全部」批量提交

### Excel 数据格式要求

| 姓名 | 部门 | 入职日期 | 工号 |
|------|------|---------|------|
| 张三 | 研发部 | 2026-01-15 | EMP001 |
| 李四 | 产品部 | 2026-01-16 | EMP002 |

> **注意**：列顺序必须与 `COLUMNS` 定义一致，日期格式为 `YYYY-MM-DD`。

---

## 示例 3：批量提交结果

### 成功场景

```
提交完成：10 条成功，0 条失败
```

- 所有行背景变绿，显示 ✓
- 草稿自动清除
- 表格重置为 3 个空行

### 部分失败场景

```
提交完成：8 条成功，2 条失败（请修正红色行后重新提交）
```

- 失败行背景变红，显示具体错误信息
- 成功行不可再编辑（已提交）
- 用户修正失败行后可再次点击「✓ 提交全部」（仅重试失败行）

---

## 示例 4：草稿恢复

用户填写了 5 行数据后意外关闭页面，重新打开后：

1. 系统自动从 `localStorage` 读取草稿数据
2. 恢复之前填写的 5 行数据
3. 用户可继续编辑或直接提交

> 草稿 key：`yida_table_form_draft_{formUuid}`，存储在浏览器本地，不跨设备同步。
