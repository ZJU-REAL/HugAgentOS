# 表单权限配置

## 严格禁止 (NEVER DO)

- 不要在未查询现有权限组的情况下直接创建新权限组（先用 get-permission 查询）
- 字段权限使用 `--field-permission` 透传宜搭 `fieldPermit` JSON；修改前必须先 `get-permission` 查看当前结构
- 不要使用自定义部门或自定义过滤条件（不支持）

## 严格要求 (MUST DO)

- 修改权限前先用 `openyida get-permission` 查询现有配置
- action-permission 为完全替换模式，只保留 true 的项，操作前确认影响范围
- **本技能不读写 memory**：权限配置通过 CLI 命令写入宜搭平台，不依赖跨会话的 memory 状态

## 适用场景

用户需要"配置权限"、"设置数据权限"、"添加权限组"、"控制谁能看/改/删数据"时使用。

## 触发条件

**正向触发**：
- "配置权限"、"设置数据权限"
- "添加权限组"、"控制谁能看/改/删数据"
- "设置成员权限"、"配置操作权限"、"配置字段权限"

## 危险操作确认

修改 action-permission 会完全替换现有操作权限配置，执行前必须：
1. 展示当前权限配置
2. 展示修改后的权限配置
3. 等待用户确认

---


## 查询权限组

```bash
openyida get-permission <appType> <formUuid>
```

## 更新权限组

```bash
openyida save-permission <appType> <formUuid> [选项]
```

| 选项 | 说明 |
|------|------|
| `--data-permission <json>` | 修改数据权限范围 |
| `--action-permission <json>` | 修改操作权限（完全替换，只保留 true 的项） |
| `--field-permission <json>` | 修改字段权限，传入宜搭 `fieldPermit` 对象或 `{ "role": "DEFAULT", "fieldPermit": {...} }` |
| `--members <userIds>` | 修改成员，多个 userId 逗号分隔 |

### 数据权限 `dataRange` 可选值

| 值 | 说明 |
|----|------|
| `ALL` | 全部数据 |
| `SELF` / `ORIGINATOR` | 本人提交 |
| `DEPARTMENT` / `ORIGINATOR_DEPARTMENT` | 本部门提交 |
| `SAME_LEVEL_DEPARTMENT` | 同级部门 |
| `SUBORDINATE_DEPARTMENT` | 下级部门 |

### 操作权限 key

`OPERATE_VIEW`、`OPERATE_EDIT`、`OPERATE_DELETE`、`OPERATE_HISTORY`、`OPERATE_COMMENT`、`OPERATE_PRINT`、`OPERATE_CREATE`、`OPERATE_BATCH_EDIT`、`OPERATE_BATCH_EXPORT`、`OPERATE_BATCH_IMPORT`、`OPERATE_BATCH_DELETE`、`OPERATE_BATCH_PRINT`、`OPERATE_BATCH_DOWNLOAD`、`OPERATE_BATCH_DOWNLOAD_QRCODE`

## 新增权限组

```bash
openyida save-permission <appType> <formUuid> --create --name <名称> [选项]
```

示例：

```bash
openyida save-permission APP_XXX FORM-XXX \
  --create --name "部门数据查看组" \
  --members "54255850977641" \
  --data-permission '{"dataRange":"ORIGINATOR_DEPARTMENT"}' \
  --action-permission '{"operations":{"OPERATE_VIEW":true}}' \
  --field-permission '{"fieldRange":"FORM"}'
```

## 字段权限

- 默认结构通常是 `{ "fieldRange": "FORM" }`，表示继承表单设计中组件状态。
- 需要自定义字段权限时，先执行 `openyida get-permission <appType> <formUuid>` 获取现有权限组中的 `fieldPermit`，在此基础上修改后传给 `--field-permission`。
- 更新已有权限组时可在 JSON 顶层带 `role`，例如 `{"role":"DEFAULT","fieldPermit":{"fieldRange":"FORM"}}`。

## 限制

- 不支持自定义部门和自定义过滤条件

## 异常处理

| 异常场景 | 处理方式 |
|---------|----------|
| get-permission 返回空 | 确认 appType 和 formUuid 正确，该表单可能尚未配置权限组 |
| save-permission 失败 | 检查 userId 格式是否正确，确认登录态有效 |
| action-permission 误操作 | 该操作为完全替换，执行前必须展示当前配置并获得用户确认 |
| 不支持的 dataRange 值 | 只能使用文档中列出的 5 种 dataRange 值，不支持自定义 |

## Agent 错误处理策略

当 Agent 执行本技能遇到错误时，必须遵循以下默认行为：

| 错误类型 | 默认处理策略 |
|---------|-------------|
| 命令执行失败 | 停止执行，向用户展示错误信息，询问是否重试或调整参数 |
| 参数缺失（appType/formUuid/userId 等） | 主动询问用户补充，不得猜测或编造 |
| 权限不足 / 登录态失效 | 停止执行，提示用户执行 `openyida auth status` 检查登录态 |
| 修改前未查询现有配置 | 必须先执行 `get-permission` 查询，再执行修改操作 |
| 网络超时 | 重试 1 次，仍失败则停止并提示用户检查网络 |
| 未知错误 | 停止执行，完整展示错误信息，建议用户反馈问题 |
