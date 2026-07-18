
# 代理中心管理

## 严格要求 (MUST DO)

- 创建、更新、撤销代理会影响真实流程审批/提交责任，执行前必须向用户确认被代理人/离职人、代理人、代理类型、时间范围和影响范围。
- 同名人员必须先用 `agent-center search-user` 或 `corp-manager search-user` 确认 userId 和部门路径，不得只凭姓名操作。
- 离职代理只有平台管理员可操作；如果接口返回无权限，不要重试，提示用户切换管理员账号或联系平台管理员。
- 普通在职代理必须传 `--start` 和 `--end`；离职代理不要传时间，平台按离职状态生效。
- 部分流程代理使用 `--range part` 时，必须提供 `--range-form <appType:formUuid>` 或 `--range-json`/`--range-file`。

## 常用命令

查询代理列表：

```bash
openyida agent-center list --status EFF --page 1 --size 20
```

搜索人员，确认 userId：

```bash
openyida agent-center search-user "余浩" --dept "宜搭,钉钉官方同学"
```

新增在职代理（默认代处理流程）：

```bash
openyida agent-center create \
  --source-user <被代理人userId> \
  --target-user <代理人userId> \
  --start "2026-05-20 09:00" \
  --end "2026-05-21 18:00" \
  --notify-source y
```

新增代提交流程代理：

```bash
openyida agent-center create \
  --source-user <被代理人userId> \
  --target-user <代理人userId> \
  --category start \
  --start "2026-05-20 09:00" \
  --end "2026-05-21 18:00"
```

新增部分流程代理：

```bash
openyida agent-center create \
  --source-user <被代理人userId> \
  --target-user <代理人userId> \
  --start "2026-05-20 09:00" \
  --end "2026-05-21 18:00" \
  --range part \
  --range-form APP_xxx:FORM_xxx,APP_xxx:FORM_yyy
```

新增离职代理：

```bash
openyida agent-center create \
  --type departure \
  --source-user <离职人userId> \
  --target-user <代理人userId>
```

编辑代理人：

```bash
openyida agent-center update <agentUuid> --target-user <新代理人userId> --type normal
```

撤销代理：

```bash
openyida agent-center cancel <agentUuid> --type normal
openyida agent-center cancel <agentUuid> --type departure
```

查询部分流程范围：

```bash
openyida agent-center range <agentUuid>
```

## 参数映射

| CLI 参数 | 页面含义 | 接口字段 |
|---------|----------|----------|
| `--source-user` | 被代理人 / 离职人 | `sourceActionerId` |
| `--target-user` | 代理人 | `toActionerId` |
| `--type normal` | 新增代理 | `agentType=NORMAL` |
| `--type departure` | 离职设置 | `agentType=DEPARTURE` |
| `--category execute` | 代处理流程 | `agentCategory=EXECUTE` |
| `--category start` | 代提交流程 | `agentCategory=START` |
| `--notify-source y` | 代理任务通知被代理人 | `originIsView=y` |
| `--range all` | 全部流程 | `agentRangeType=ALL` |
| `--range part` | 部分流程 | `agentRangeType=PART` |

## 状态映射

| 状态 | 页面含义 |
|------|----------|
| `ALL` | 全部 |
| `DIS` | 待生效 |
| `EFF` | 代理中 |
| `OUT` | 已过期 |
| `CANCEL` | 已撤销 |

## 安全检查清单

1. `agent-center search-user` 确认被代理人/离职人和代理人的 userId、部门路径。
2. `agent-center list --keyword <userId>` 查看是否已有代理记录。
3. 展示将要执行的 create/update/cancel 命令和影响范围。
4. 用户确认后执行。
5. 执行后用 `agent-center list --keyword <userId>` 验证状态。
