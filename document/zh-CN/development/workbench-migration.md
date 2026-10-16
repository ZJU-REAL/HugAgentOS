# 工作台兼容与迁移

[English](../../en/development/workbench-migration.md)

| 契约 | 兼容范围 |
| --- | --- |
| SDK / 协议 1.0.0 | 能力协商后的 UI v3 工作区页面 |
| 既有 UI v1 / v2 | 既有画布及工具结果桥接 |
| 既有业务 /v1/applications | 原托管业务应用域 |
| 新 /v1/workbench | 目录、启动及应用作用域操作 |

SDK、协议、UI 和插件业务版本分别记录。
旧 UI 声明保留原契约。
新增工作区页面声明 UI v3、workspace 模块及引用该模块的应用。
打包包内 web 资产，校验引用及宿主支持的必需能力。
不得伪造 conversation ID 作为应用资源所有者。

应用键是目录返回的不透明引用。
它绑定准确的本机/云端执行来源、不透明服务来源、完整安装 SHA-256 和包含微秒的 UTC 创建时间。
升级保留安装键；卸载后重装生成新键。
导航和权限选择器均使用准确目录键。
不得按名称、slug、服务地址或应用 ID 复用授权。
账号、安装、执行来源或版本改变后须重新鉴权。

## 安全迁移步骤

1. 盘点旧 ID、重复项、显式偏好、角色继承、认证及 UI 版本。
2. 对配置、数据和安装包建立一致且受保护的快照。
3. 在测试副本执行只读预检查；隔离未知项并报告。
4. 保留外部应用稳定映射及用户已有显式偏好。
5. 将可见性与业务 REST 权限分开，不扩大授权。
6. 仅执行已授权迁移，使用 revision 检查及回执。
7. 在测试副本重入操作，检查幂等性。
8. 使用相应数据库时，验证 SQLite 和 PostgreSQL 升级及恢复。
9. 验证真实账号、启动、数据/文件、撤销及既有用户流程。
10. 生产迁移须取得单独授权。

读取失败不等于没有偏好。
保留未知项及错误证据，不通过删除未知项获得成功。
新增写入后，仅恢复旧快照会丢失数据。
降级前冻结写入、导出增量，并核对旧格式能否表达新数据。
旧格式不能表达时，停止降级，恢复已验证的新版本。
SDK CLI 只校验声明，不迁移数据库或业务数据。

# 工作台配置迁移与恢复

迁移是显式运维操作，不会随启动自动执行。先在隔离数据库副本完成演练；真实配置迁移需要单独授权。当前命令只接受本机隔离副本。

## 盘点与切换

1. 停止副本的所有写入，包括内容导入、用户偏好、插件安装和后台写入。--writes-frozen 只是操作员确认，不会自动停止服务。
2. 配置 WB20_COPY_DATABASE_URL。SQLite 文件须在当前源码目录的 .scratch/workbench-20 下。PostgreSQL 须为回环主机且数据库名以 workbench_20 开头。不要将真实库设置为演练目标。
3. 创建权限为 0700 的快照目录。运行如下命令：

```bash
PYTHONPATH=src/backend python -m scripts.workbench_migration inventory \
  --snapshot /absolute/private/path/before.json
PYTHONPATH=src/backend python -m scripts.workbench_migration apply \
  --snapshot /absolute/private/path/before.json --digest <reviewed-digest> --writes-frozen
```

盘点不写数据库。报告包含数量、来源版本、脱敏位置和错误码；不打印 URL、用户标识、票据或秘密。完整迁移范围快照保存在 0600 文件中，包含原配置、偏好、角色/团队关系、插件声明和工作台数据。它不是业务文件或完整数据库备份，须与完整数据库/文件备份共同保管。

摘要匹配且源未变化时，切换在单一事务中执行。失败回滚事务。切换前后快照的摘要记录在 workbench_migration:v1 receipt 中。原始 app_config 行和旧导航列表移除；新目录只读取规范来源。重复外部 ID 的全部副本、未知 ID、危险 URL 和未知字段隔离，不产生入口或授权。原始副本保留在受保护快照。

## 保留的语义

- 旧外部入口继续使用 legacy:<id>。allowed_apps 仍使用旧可见性 ID，个人 null/缺失继承、空列表拒绝和 * 强制全部、角色联合与团队默认不变。迁移不会把可见权限转换为业务 REST 执行权限。
- 外部条目顺序和启停保留。默认侧栏转换为策略。已有现代偏好优先；已确认旧 preferences.pinned_apps/app_order 转换为 web 偏好。null 继承，空列表明确取消固定。聊天排序、主题和其他偏好保持不变。
- 无法映射的权限引用保留原值并报告，不成为新授权。旧插件 UI v1/v2 不改写为 v3。不会按名称或 slug 绑定新安装。
- 旧认证配置 workbench_external_auth:legacy:<id> 保持原 key。缺少目标侧适配时报告 auth_configuration_required。管理员须显式审核登录方式；真实旧目标尚未验证时，不得宣称外部登录无回归。不得恢复通用 SSO token 拼接。
- 同服务恢复保留 workbench_origin_identity。克隆到另一服务须先在克隆副本重新盘点，再执行 clone-origin；旧插件 key 不自动映射为新来源授权。跨来源偏好和数据须另行核对。

## 对外版本窗口

content-v1 接口在工作台协议 1.x 内保留，协议 2.0 迁移前另行公告。旧 app_config 和 page_config 写入口、内容导入/导出由版本适配直接访问规范来源；不维护第二份旧表或双写。未知字段写入会明确拒绝。管理员新外部配置与旧入口分别展示，但读取同一规范存储。

## 恢复与新增数据

先冻结全部写入，再执行：

```bash
PYTHONPATH=src/backend python -m scripts.workbench_migration rollback \
  --snapshot /absolute/private/path/before.json --digest <reviewed-digest> \
  --delta /absolute/private/path/delta.json --writes-frozen
```

恢复前导出受保护增量。已映射普通旧外链的修改可回放到旧格式。新身份、应用状态、资源、权限、插件、认证或策略变化不能安全表达时，返回 delta_not_representable 并保留新数据。不要仅恢复旧快照。继续运行已验证的新版本，核对增量并另行制定迁移方案。恢复 receipt 幂等；已恢复批次不能直接重复 apply，应创建新的审核批次。

真实数据库副本、PostgreSQL/SQLite、旧登录、两种侧栏及原生客户端须分别验收。单元或 HTTP fixture 通过不等于完整升级验收。
