
# PostgreSQL Sequence 自动修复

## 严格禁止 (NEVER DO)

- 不要在生产环境直接执行 `--fix`，必须先用 `--dry-run` 预览
- 不要在未确认数据库连接的情况下执行修复操作
- 不要跳过 Sequence 检测直接修改数据库

## 严格要求 (MUST DO)

- 执行修复前必须先用 `--dry-run` 确认影响范围
- 修复完成后必须验证 Sequence 值是否正确更新
- 记录修复操作日志，便于问题追溯

## 适用场景

| 用户意图 | 触发条件 |
|---------|---------|
| Sequence 起始值检测 | 环境检测自动建表后，需要验证 Sequence 配置 |
| 主键冲突修复 | 插入数据时报 `duplicate key value violates unique constraint` 错误 |
| 预防性检查 | 数据迁移后，确保 Sequence 值正确 |

---

## 背景

宜搭环境检测自动建表时，PostgreSQL 的 Sequence 默认使用 `START 1`。如果数据迁移时已有数据，Sequence 起始值可能小于已有数据的最大 ID，导致插入时报主键冲突错误。

此工具用于：
1. 检测 8 个核心表的 Sequence 当前值
2. 对比表中实际最大 ID
3. 自动将 Sequence 起始值设置为 `max(id) + 1`

## 命令

### 预览模式（推荐先执行）

```bash
openyida db-seq-fix --dry-run
```

输出示例：
```
📋 Sequence 检测报告

表名                    当前值    最大ID    状态
─────────────────────────────────────────────
form_component          1         1000      ⚠️ 需要修复
form_definition         1         500       ⚠️ 需要修复
...

💡 使用 openyida db-seq-fix --fix 执行自动修复
```

### 执行修复

```bash
openyida db-seq-fix --fix
```

输出示例：
```
✅ Sequence 修复完成

表名                    修复前    修复后
────────────────────────────────────────
form_component          1         1001
form_definition         1         501
...
```

## 监控的表

| 表名 | Sequence 名称 |
|------|--------------|
| form_component | form_component_id_seq |
| form_definition | form_definition_id_seq |
| form_instance | form_instance_id_seq |
| form_permission | form_permission_id_seq |
| process_definition | process_definition_id_seq |
| process_instance | process_instance_id_seq |
| process_task | process_task_id_seq |
| system_log | system_log_id_seq |

## 参数说明

| 参数 | 说明 |
|------|------|
| `--dry-run` | 仅检测，不执行修复，输出检测报告 |
| `--fix` | 执行自动修复，将 Sequence 起始值设置为 `max(id) + 1` |

## 错误处理

| 错误信息 | 原因 | 处理方式 |
|---------|------|---------|
| `数据库连接失败` | 数据库配置错误或服务不可用 | 检查数据库连接配置 |
| `表不存在` | 表尚未创建 | 先执行环境检测建表 |
| `Sequence 不存在` | Sequence 未正确创建 | 手动创建 Sequence |

## 注意事项

1. **生产环境谨慎操作**：建议在低峰期执行，避免影响业务
2. **备份数据**：修复前建议备份数据库
3. **验证结果**：修复后执行测试插入，确认问题已解决
4. **日志记录**：修复操作会记录到系统日志，便于追溯