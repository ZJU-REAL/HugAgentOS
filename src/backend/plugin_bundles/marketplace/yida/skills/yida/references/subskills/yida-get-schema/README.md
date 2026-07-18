
# 获取表单 Schema

## 严格禁止 (NEVER DO)

- **绝对禁止猜测或编造 fieldId**，宜搭字段 ID 由平台随机生成（格式如 `textField_eftt1aa5m`、`selectField_fix024y92`），无法从字段名称推断，必须通过此命令获取
- 不要在未获取 Schema 的情况下执行任何涉及字段 ID 的操作
- 不要假设字段 ID 格式，即使看起来像 `textField_xxx`，也必须通过命令确认
- 不要跳过 Schema 获取步骤直接进行数据操作，即使用户催促也必须先获取
- 不要在 Schema 获取失败时继续执行后续操作，必须先解决问题
- 不要缓存过期的 Schema 信息，表单结构变更后必须重新获取
- 不要把 `openyida get-schema` 的 stdout 通过 shell 重定向保存成 JSON，也不要用 heredoc、`cat`/`echo`/`printf`/`tee` 生成 Schema 文件

## 严格要求 (MUST DO)

- **凡是需要用到字段 ID（fieldId）的操作，必须先执行此命令**，不得跳过
- 将关键字段 ID 映射（字段名 → fieldId）用结构化文件写入工具记录到 `<projectRoot>/.cache/<项目名>-schema.json`，供后续操作复用
- 如需保存完整 Schema 文件，先执行命令获取 stdout，再用 create_file / Write / file edit tool 写入 `<projectRoot>/.cache/openyida/<项目名或任务名>/<表单名>-schema.json`；从 workspace 根执行后续命令时传 `project/.cache/...`
- **录入/更新数据后，必须用 `openyida data query --size 1` 抽查一条记录，确认 `formData` 中字段有实际值（非空 `""`），若全部为空说明字段 ID 有误，需重新排查**

## 适用场景

在执行以下操作前**必须**使用：
- **新增/录入表单数据**（`yida-data-management` create）← 最常见的遗漏场景，必须先 get-schema
- 更新表单数据（`yida-data-management` update）
- 配置数据查询条件（`yida-data-management` query searchFieldJson）
- 更新表单字段结构（`yida-create-form-page` update 模式）
- 配置流程字段权限（`yida-process-rule`）
- 自定义页面中引用字段 ID 常量（`yida-custom-page`）

## 触发条件

**正向触发**：
- 任何需要用到 fieldId 的操作前（自动前置触发）
- "查看表单结构"、"获取字段 ID"、"查看 Schema"
- 其他技能（yida-data-management、yida-process-rule、yida-custom-page）执行前的前置步骤
- "批量获取所有表单 Schema"、"导出应用下所有字段 ID"、"不知道 formUuid 先全量看一遍"

---


## 命令

```bash
openyida get-schema <appType> <formUuid>
openyida get-schema <appType> --all [--output-dir <dir>] [--keyword <text>] [--concurrency N] [--retries N]
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `appType` | 是 | 应用 ID |
| `formUuid` | 单表模式必填 | 表单 UUID |
| `--all` | 批量模式必填 | 获取应用下所有表单/页面的 Schema |
| `--output-dir <dir>` | 否 | 将每个 Schema 写入独立 JSON 文件，并生成 `index.json` |
| `--keyword <text>` | 否 | 仅批量导出名称、UUID、类型或路径匹配关键词的表单 |
| `--concurrency N` | 否 | 批量并发数，默认 3，范围 1-10 |
| `--retries N` | 否 | 单个 Schema 失败后的重试次数，默认 1，范围 0-5 |

### 单表模式

```bash
openyida get-schema APP_XXX FORM-XXX
```

如需复用输出，使用 agent 的结构化文件写入工具创建：

```text
<projectRoot>/.cache/openyida/customer/customer-schema.json
<projectRoot>/.cache/customer-schema.json   # 仅保存 appType/formUuid/fieldId/reportId 等 ID 映射
```

### 批量模式

```bash
openyida get-schema APP_XXX --all --output-dir .cache/openyida/customer/schemas
openyida get-schema APP_XXX --all --keyword 客户 --concurrency 5 --retries 2
```

批量模式会先读取应用导航中的表单/页面列表，再逐个请求 `getFormSchema`。如果指定 `--output-dir`：

- 每个成功的 Schema 会写入 `<表单名>-<formUuid>.json`
- `index.json` 会记录 `formUuid`、名称、类型、字段摘要、失败原因和 schema 文件路径
- stdout 仍输出汇总 JSON，便于脚本继续处理

## 输出

单表模式将完整的 Schema JSON 输出到 stdout，包含 `pages`、`componentsMap` 等字段结构。

批量模式将汇总 JSON 输出到 stdout；指定 `--output-dir` 时，完整 Schema 写入文件，stdout 中的 `forms[].schemaFile` 指向对应文件。

> 编码前可用此命令确认表单中各字段的 `fieldId`。

## 异常处理

| 异常场景 | 处理方式 |
|---------|----------|
| 命令返回失败 | 确认 appType 和 formUuid 正确，检查登录态 |
| 输出被终端截断 | 重新执行后将 stdout 通过结构化文件写入工具保存到 `<projectRoot>/.cache/openyida/<项目名或任务名>/<表单名>-schema.json`；不要使用 shell 重定向 |
| 需要多个表单字段 ID | 使用批量模式：`openyida get-schema <appType> --all --output-dir .cache/openyida/<项目名或任务名>/schemas` |
| 批量部分失败 | 查看 stdout 的 `failedCount` 和 `forms[].errorMsg`，必要时提高 `--retries` 或缩小 `--keyword` 范围 |
| 找不到目标字段 | 检查字段是否已创建，字段 ID 格式如 `textField_xxxxxxxx`，不能手写猜测 |
| Schema 输出为空 | 表单可能没有字段，先用 `yida-create-form-page` 创建字段 |

## 与其他技能的配合

| 步骤 | 技能 | 说明 |
|------|------|------|
| 前置 | `yida-create-form-page` | 先创建表单，再获取字段 ID |
| 前置 | `yida-login` | 确认登录态有效 |
| 后续 | `yida-data-management` | 使用 fieldId 查询/新增/更新数据 |
| 后续 | `yida-process-rule` | 使用 fieldId 配置流程字段权限 |
| 后续 | `yida-custom-page` | 在 JSX 代码中引用 fieldId 常量 |
