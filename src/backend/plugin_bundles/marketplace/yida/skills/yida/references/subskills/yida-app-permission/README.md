
# 应用级权限设置

## 严格要求 (MUST DO)

- 修改前先执行 `openyida app-permission get <appType>` 查询当前应用管理员设置。
- 添加成员前先用 `search-user` 确认目标人员 userId；同名人员必须结合部门信息区分。
- 修改 `main` 应用主管理员会影响应用后台最高管理权限，执行前必须展示当前成员和修改后成员，并等待用户确认。
- `main` 应用主管理员不能为空；清空仅允许 `data` 或 `dev`。

## 常用命令

查询应用管理员设置：

```bash
openyida app-permission get <appType>
```

搜索人员，确认 userId：

```bash
openyida app-permission search-user "姓名或关键词" --dept "部门关键词"
```

添加成员：

```bash
openyida app-permission add <appType> main --users <userId>
openyida app-permission add <appType> data --users <userId1,userId2>
openyida app-permission add <appType> dev --users <userId1,userId2>
```

完全替换某一类成员：

```bash
openyida app-permission set <appType> main --users <userId1,userId2>
openyida app-permission set <appType> data --users <userId1,userId2>
openyida app-permission set <appType> dev --users <userId1,userId2>
```

移除成员：

```bash
openyida app-permission remove <appType> data --users <userId>
openyida app-permission remove <appType> dev --users <userId>
```

清空数据管理员或开发成员：

```bash
openyida app-permission set <appType> data --clear
openyida app-permission set <appType> dev --clear
```

## 角色映射

| CLI 角色 | 页面含义 | 接口 adminType |
|---------|----------|----------------|
| `main` | 应用主管理员 | `MAIN` |
| `data` | 数据管理员 | `DATA` |
| `dev` | 开发成员 | `DEV` |

## 安全检查清单

1. `app-permission search-user` 确认目标 userId 和人员身份。
2. `app-permission get` 查询当前应用管理员配置。
3. 展示将要执行的 `add` / `remove` / `set` 命令和修改后 userId 列表。
4. 用户确认后执行修改。
5. 再次 `app-permission get` 验证结果。

## 异常处理

| 异常场景 | 处理方式 |
|---------|----------|
| 权限不足 / 登录态失效 | 停止执行，提示用户执行 `openyida login` 重新登录或切换具备权限的账号 |
| 同名人员 | 必须通过部门路径或 userId 二次确认 |
| 试图清空应用主管理员 | 拒绝执行，要求至少保留 1 个 `main` 成员 |
| 修改前未查询现有配置 | 先执行 `app-permission get`，不要直接修改 |
