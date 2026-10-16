# 插件升级与权限审批

升级保留同一安装的工作台入口。卸载后重装属于新安装，不能继承旧入口授权。

升级增加应用权限、模块 grants、后台保活，或改变数据源和资源目标时，宿主暂存候选包。当前安装继续使用已验证的旧版本。私有插件所有者在插件详情中查看新增权限，选择“批准升级”或“保留旧版本”。全局插件由管理员在管理台审批。批准不会自动开启原来停用的插件。

状态 schema 变化需要明确确认使用全新状态。未确认时保留旧安装；不把旧状态解释成新 schema。不兼容的宿主要求、无效声明和缺失资产会阻止替换。

运行实例使用精确 package revision。旧实例的静态资产通过 90 秒租约保留，每 30 秒续期。新实例使用当前 revision。关闭的实例立即失去访问权；旧资产在实例关闭且租约到期后才由资源清理器回收。撤销、卸载和权限减少不会等待资产保留期。

API 使用认证上下文中的用户，不接受调用者指定其他用户：

- `GET /v1/workbench/plugin-upgrades/{install_id}`：当前 revision、候选 revision、权限差异和状态 schema 变化。
- `POST /v1/workbench/plugin-upgrades/{install_id}/decision`：`expectedRevision`、`candidateRevision`、`decision=approve|reject`、可选 `resetState`。
- 全局安装使用 `/v1/workbench/admin/plugin-upgrades/...`，需要既有管理员权限。
- `POST/PUT /v1/workbench/applications/{key}/revision-leases`：绑定当前用户、完整应用 key、revision 和 instanceId。
- `POST .../revision-leases/close`：重复关闭安全。
- 包资产请求的 `instanceId` 只引用已建立的租约，不能为旧 revision 创建新租约。

审批请求必须匹配当前安装和当前候选包。过时请求返回 `revision_conflict`；没有待审批包返回 `upgrade_not_pending`；未确认状态重置返回 `state_reset_confirmation_required`。临时目录和候选包不包含额外保存的审批凭据。

候选存储限制：每安装最多 2 个、100 MiB；每所有者最多 8 个、200 MiB。云端计入压缩包与解压缓存的容量上界，本机计入 bundle 与 inventory。替换或拒绝会安全回收无引用的候选；当前版本、旧实例租约、历史和其他安装引用保持。清理失败继续占用配额，可重试；超额返回 413 candidate_storage_quota_exceeded，不改变当前或待审批版本。本机候选批准前不生成技能子资产。卸载后的候选也会回收，反复重装不能绕过所有者配额。

## 续租状态与连接恢复

版本租约由独立状态机管理。收到租约响应后，宿主校验 instanceId、revision 和 expiresAt，并同时以服务端到期时间与本机单调时钟限制有效期。请求有 10 秒超时。

续租遇到网络故障、408、429 或暂时性 5xx 时，保留已加载页面并暂停应用能力，按有限退避重试。重试不会延长最后一次已确认租约的有效期。恢复成功后继续使用同一实例；401、403、404、409、410 等确定性拒绝立即终止实例。租约到期、关闭或身份切换后，迟到响应不能恢复实例。

恢复期间取消正在执行的桥接调用，并在每次新调用时再次检查租约。页面显示连接恢复状态。窗口恢复或系统时钟变化不能让已过期租约重新获得能力。
