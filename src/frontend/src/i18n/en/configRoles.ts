/** 英文字典（角色权限域）：key 为中文原文，value 为英文译文。 */
export const CONFIG_ROLES_DICT: Record<string, string> = {
  // ── 菜单 / 面板 ─────────────────────────────────────
  '角色权限': 'Roles',
  '新建角色': 'New Role',
  '编辑角色': 'Edit Role',
  '角色名称': 'Role Name',
  '如：部门管理员': 'e.g. Department Admin',
  '角色用途说明（可选）': 'Role description (optional)',
  '角色已创建': 'Role created',
  '授予': 'Grant',
  '授予能力': 'Granted Capabilities',
  '限定应用': 'App-restricted',
  '已分配': 'Assigned',
  '确认删除（该角色的全部分配将一并清除）？': 'Delete this role (all its assignments will be removed)?',
  '角色 = 可复用的命名能力包，可分配给部门（团队）或个人。能力解析：个人逐项设置 → 角色（并集） → 团队默认 → 系统默认。分配入口在「用户管理」「团队管理」。':
    'A role is a reusable named capability bundle, assignable to departments (teams) or individuals. Resolution: personal per-item → roles (union) → team defaults → system defaults. Assign roles from User / Team Management.',
  '授予后，拥有该角色者默认即可免 token 直达对应后台并拥有完整权限。请谨慎授予。':
    'Once granted, holders of this role can reach the corresponding console without a token and with full privileges. Grant with care.',
  '不限定': 'Unrestricted',
  '限定可见应用': 'Restrict visible apps',
  '勾选该角色授予可见的应用。': 'Select the apps this role grants visibility to.',
  // ── 能力描述（授予语义） ─────────────────────────────────────
  '授予「实验室」入口与代码执行等实验功能。': 'Grant access to the Lab entry and experimental features like code execution.',
  '授予在设置中心创建 API-Key 调用智能体。': 'Grant creating API-Keys in Settings to call the agent.',
  '授予在能力中心自助上传私有技能。': 'Grant self-service uploading of private skills.',
  '授予自助添加私有远程 MCP 工具。': 'Grant self-service adding of private remote MCP tools.',
  '授予安装内置插件、导入插件包。': 'Grant installing built-in plugins and importing plugin packages.',
  '授予自建子智能体、从市场安装与申请上架。': 'Grant building sub-agents, installing from and submitting to the marketplace.',
  '授予创建私有知识库（仅本人可见）。': 'Grant creating private knowledge bases (visible only to the owner).',
  '授予创建公有知识库（默认全员可见，可再授权限定）。': 'Grant creating public knowledge bases (visible to all by default, scopable by grants).',
  '授予自助绑定飞书等渠道机器人（以本人身份运行）。': 'Grant self-service binding of channel bots like Lark (running as the user).',
  '模型切换': 'Model Switch',
  '开放后，用户可在对话输入框中切换当前启用的对话模型。':
    'When enabled, the user can switch among currently enabled chat models in the chat input.',
  '团队成员默认是否可在对话输入框中切换当前启用的对话模型。':
    'Whether team members can switch among currently enabled chat models in the chat input by default.',
  '授予在对话输入框中切换当前启用的对话模型。':
    'Grant switching among currently enabled chat models in the chat input.',
  '授予免 token 直达「系统配置」后台并拥有完整权限。': 'Grant token-free access to the System Config console with full privileges.',
  '授予免 token 直达「内容管理」后台并拥有完整权限。': 'Grant token-free access to the Content Management console with full privileges.',
  '授予其智能体装「安全管理」插件后只读查询全局审计日志、调用日志、沙盒状态与系统健康（纯只读，无处置动作）。':
    'Grant the role\'s agents read-only querying of global audit logs, call logs, sandbox status and system health once the Security Manager plugin is installed (read-only; no remediation actions).',
  // ── 角色分配弹窗 ─────────────────────────────────────
  '角色分配 · {name}': 'Role Assignment · {name}',
  '为部门（团队）分配默认角色，成员实时继承（多角色取并集）。':
    'Assign default roles to the department (team); members inherit them in real time (multiple roles are unioned).',
  '为该用户分配角色（多角色取并集）。个人逐项权限设置优先于角色。':
    'Assign roles to this user (multiple roles are unioned). Personal per-item settings take precedence over roles.',
  '尚未创建任何角色': 'No roles created yet',
  '{n} 项能力': '{n} capabilit(ies)',
  '该角色尚未分配给任何用户或团队': 'This role has not been assigned to any user or team',
  // ── 权限配置弹窗内「按权限 / 按角色」下拉 ─────────────────────────────────────
  '配置方式': 'Configure by',
  '按权限逐项配': 'By permission',
  '按角色配': 'By role',
  '为部门（团队）分配默认角色，成员实时继承（多角色取并集）。角色模板在「角色权限」页维护。':
    'Assign default roles to the department (team); members inherit them in real time (roles are unioned). Role templates are maintained on the Roles page.',
  '为该用户分配角色（多角色取并集）。个人逐项权限设置优先于角色。角色模板在「角色权限」页维护。':
    'Assign roles to this user (roles are unioned). Personal per-item settings take precedence over roles. Role templates are maintained on the Roles page.',
  // ── 应用可见范围三态 ─────────────────────────────────────
  '跟随团队/角色': 'Follow team/role',
  '全部应用': 'All apps',
  '跟随团队/角色的应用范围；都未限定时即全部应用。':
    'Follows the team/role app scope; if neither restricts, all apps are visible.',
  '个人强制可见全部应用，覆盖团队/角色的限定。':
    'Personal override: see all apps, overriding any team/role restriction.',
  '个人仅可见下方勾选的应用，覆盖团队/角色。':
    'Personal: only the apps checked below are visible, overriding team/role.',
  // ── 新团队默认角色 ─────────────────────────────────────
  '新团队默认': 'New-team default',
  '新团队默认角色': 'Default role for new teams',
  '开启后，新建团队 / OA·SSO 同步建团队时自动把本角色设为该团队默认（成员继承）。':
    'When enabled, this role is auto-assigned as the default for any newly created or OA/SSO-synced team (members inherit it).',
};
