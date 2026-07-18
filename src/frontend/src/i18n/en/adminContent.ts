/** 英文字典（adminContent 域）：key 为中文原文，value 为英文译文。 */
export const ADMIN_CONTENT_DICT: Record<string, string> = {
  '「{name}」上传失败：{msg}': '"{name}" upload failed: {msg}',
  '下载失败：{msg}': 'Download failed: {msg}',
  '加载分块失败：{msg}': 'Failed to load chunks: {msg}',
  '加载文档失败：{msg}': 'Failed to load documents: {msg}',
  '卡片 ID 重复：{msg}': 'Duplicate card ID: {msg}',
  '生成失败：{msg}': 'Generation failed: {msg}',
  '重建索引失败：{msg}': 'Reindex failed: {msg}',
  // PageConfigEditor — 通用
  '加载页面配置失败': 'Failed to load page config',
  '上传成功': 'uploaded successfully',
  '发布': 'Publish',
  '重新加载': 'Reload',
  '请检查必填项': 'Please fill in all required fields',
  '必填': 'Required',
  '页面配置已发布，所有用户将在 15 秒内生效': 'Page config published. All users will see the changes within 15 seconds.',
  '暂无': 'None',

  // PageConfigEditor — Alert
  '页面配置会覆盖前端硬编码文案与品牌信息。发布后所有在线用户将在 15 秒内看到变化。':
    'Page config overrides hardcoded UI copy and branding. All online users will see changes within 15 seconds of publishing.',

  // PageConfigEditor — Collapse 标签
  '品牌标识': 'Branding',
  '侧边栏布局': 'Sidebar Layout',
  '导航与面板标题': 'Navigation & Panel Titles',
  '文案与占位符': 'Copy & Placeholders',
  '对话默认设置': 'Chat Defaults',
  '首页快捷方式': 'Homepage Shortcuts',

  // PageConfigEditor — 品牌区字段
  '产品名称': 'Product Name',
  '副标题': 'Subtitle',
  '浏览器标签标题（page title）': 'Browser Tab Title (page title)',
  '首页欢迎区': 'Homepage Welcome Section',
  '主标题（hero title）': 'Hero Title',
  '副标题（hero subtitle）': 'Hero Subtitle',
  '底部免责声明': 'Footer Disclaimer',
  '点击或拖拽 Logo 图片到此处': 'Click or drag Logo image here',
  '点击或拖拽 Favicon 到此处': 'Click or drag Favicon here',
  '支持 PNG/JPG/SVG/WebP，≤ 2MB': 'PNG / JPG / SVG / WebP, ≤ 2 MB',
  '支持 PNG/SVG/ICO，≤ 2MB': 'PNG / SVG / ICO, ≤ 2 MB',

  // PageConfigEditor — 导航区
  '各 panel 标题（顶部大标题）': 'Panel titles (top heading)',
  'panel 副标题（标题下方的说明文案）': 'Panel subtitles (below the heading)',
  '留空表示该 panel 不显示副标题': 'Leave blank to hide the subtitle for that panel',
  '（可选）': '(optional)',

  // PageConfigEditor — 后管平台
  '后管平台': 'Admin Platform',
  '后管平台名称': 'Admin platform name',
  '各页标签名称': 'Per-page tab labels',
  '后管平台（内容管理 / 系统配置 / 接口文档）的统一产品名与各页标签；产品名决定后台页眉品牌与各页浏览器标签标题。':
    'Unified product name and per-page tab labels for the admin platform (Content / Config / API Docs); the product name drives the admin header brand and each page\'s browser tab title.',
  '内容管理、系统配置、接口文档三页共用此名称作品牌。':
    'Content Management, System Config, and API Docs all share this name as their brand.',

  // PageConfigEditor — PANEL_KEYS labels

  // PageConfigEditor — TEXT_FIELDS labels
  '主聊天输入框占位符': 'Main chat input placeholder',
  '子智能体输入框占位符': 'Sub-agent input placeholder',
  '搜索对话占位符': 'Search chat placeholder',
  '新建对话按钮': 'New chat button',
  '退出按钮': 'Sign out button',
  '历史对话标题': 'Chat history label',
  '侧边栏空状态': 'Sidebar empty state',
  '搜索无结果': 'Search no results',
  '退出确认 - 标题': 'Sign-out confirm - title',
  '退出确认 - 内容': 'Sign-out confirm - content',
  '退出确认 - 确认按钮': 'Sign-out confirm - OK button',
  '推荐横幅文案（留空则隐藏）': 'Recommend banner text (leave blank to hide)',

  // PageConfigEditor — 对话默认设置
  '控制用户每次登录与新建对话时的初始模式；用户在对话中手动切换的结果会保留到下一次新建对话。「思考·高」「思考·超高」仅对支持 reasoning_effort 的模型生效，否则下游会自动回落到「思考·中」。':
    'Controls the initial mode when users log in or start a new chat. Manual mode switches during a chat persist to the next new chat. "Think High" and "Think Max" only apply to models that support reasoning_effort; otherwise they fall back to "Think Medium".',
  '默认对话模式': 'Default Chat Mode',
  '选择新建对话时的初始档位。': 'Select the initial mode for new chats.',
  '快速模式（不思考）': 'Fast (no thinking)',
  '思考·中（默认强度）': 'Think · Medium (default)',
  '思考·高（reasoning_effort=high）': 'Think · High (reasoning_effort=high)',
  '思考·超高（reasoning_effort=max）': 'Think · Max (reasoning_effort=max)',

  // PageConfigEditor — 登录与注册
  '登录与注册': 'Login & Registration',
  '控制登录页是否开放「注册」入口。关闭后登录页不再显示注册 Tab 与注册子页，且后端会直接拒绝注册提交，仅保留登录。':
    'Controls whether the login page offers registration. When disabled, the login page hides the registration tab and form, the backend rejects registration submissions, and only login remains.',
  '允许注册': 'Allow Registration',
  '开启时登录页展示注册 Tab；关闭后仅保留登录。':
    'When enabled, the login page shows a registration tab; when disabled, only login remains.',

  // PageConfigEditor — HomepageShortcutsPanel
  '配置首页欢迎区底部的能力快捷卡。禁用后用户界面将隐藏该卡片；配置了「外链 URL」的卡片点击后在新标签页打开（自动附加 SSO token），未配置 URL 的展示「建设中」提示；id=knowledge 的卡片在未配 URL 时跳转知识库面板。':
    'Configure the shortcut cards at the bottom of the homepage welcome section. Disabled cards are hidden from users. Cards with an external URL open in a new tab (SSO token is appended automatically). Cards without a URL show a "Coming soon" message; cards with id=knowledge navigate to the Knowledge Base panel instead.',
  '新增卡片': 'Add Card',
  '首页快捷方式已保存，所有用户将在 15 秒内生效': 'Homepage shortcuts saved. All users will see the changes within 15 seconds.',
  '卡片 ID 已存在': 'Card ID already exists',
  '存在 ID 为空的卡片': 'A card has an empty ID',
  '卡片 ID 重复': 'Duplicate card ID',
  '卡片「{id}」名称为空': 'Card "{id}" has an empty name',
  '（已隐藏）': '(hidden)',
  '展示': 'Show',
  '隐藏': 'Hide',
  '删除卡片「{name}」？': 'Delete card "{name}"?',
  '删除后所有用户都将看不到该卡片。': 'Once deleted, this card will no longer be visible to any user.',
  '卡片名称': 'Card name',
  '可从默认图标库选择，或上传 PNG / JPG / SVG / WebP（最大 2MB）': 'Choose from the default icon library or upload a PNG / JPG / SVG / WebP (max 2 MB)',
  '外链 URL（可选）': 'External URL (optional)',
  '留空则点击展示「建设中」提示；id=knowledge 时留空也会跳转知识库': 'Leave blank to show a "Coming soon" message on click; for id=knowledge, blank also navigates to the Knowledge Base panel',
  '新增首页快捷方式': 'Add Homepage Shortcut',
  '卡片 ID': 'Card ID',
  '请输入 ID': 'Please enter an ID',
  '仅允许小写字母、数字、下划线': 'Only lowercase letters, digits, and underscores',
  '唯一标识；保存后不建议修改': 'Unique identifier; not recommended to change after saving',

  // PageConfigEditor — SidebarLayoutEditor
  '决定每个模块出现在「左侧栏一级导航」、收纳进「用户头像菜单」，还是完全隐藏。改动会随其他配置一起发布生效。':
    'Decide whether each module appears in the left sidebar (primary navigation), is tucked into the user avatar menu, or is hidden entirely. Changes take effect when the config is published.',
  '左侧栏（一级导航）': 'Left Sidebar (Primary Nav)',
  '（无项目，左侧栏将不渲染导航区）': '(No items — the left sidebar navigation area will not be rendered)',
  '用户头像菜单': 'User Avatar Menu',
  '（无项目，菜单将只剩「退出登录」）': '(No items — only "Sign out" will remain in the menu)',
  '需启用实验室权限': 'Requires Lab permission',
  '← 放到左侧栏': '← Move to Sidebar',
  '放到用户菜单 →': 'Move to User Menu →',
  '不显示': 'Hide',

  // KnowledgeBaseManager — 分块方法

  // KnowledgeBaseManager — 索引状态
  '索引失败原因': 'Indexing failure reason',

  // KnowledgeBaseManager — Alert
  '当前知识库后端为 Dify（只读视图）': 'Current knowledge base backend: Dify (read-only view)',
  '平台正使用 Dify 作为知识库后端，下表为 Dify 数据集列表。新增、上传、索引与文件管理请前往 Dify 后台操作。':
    'The platform is using Dify as the knowledge base backend. The table below lists Dify datasets. To add, upload, index, or manage files, use the Dify admin console.',
  '自建公共知识库': 'Self-hosted Public Knowledge Base',
  '此处管理对所有用户可见、可检索的公共知识库。支持创建库、上传文档、查看索引状态、重建索引与分块标签/问题管理。':
    'Manage public knowledge bases that are visible and searchable by all users. Supports creating bases, uploading documents, viewing index status, rebuilding indexes, and managing chunk tags/questions.',

  // KnowledgeBaseManager — 按钮
  '新建知识库': 'New Knowledge Base',
  '管理文档': 'Manage Docs',

  // KnowledgeBaseManager — 列标题
  '默认': 'Default',
  '无简介': 'No description',

  // KnowledgeBaseManager — Popconfirm / Tooltip
  '将永久删除该公共知识库及其全部文档、分块与向量数据，不可恢复。':
    'This will permanently delete the public knowledge base along with all its documents, chunks, and vector data. This action cannot be undone.',
  'Dify 知识库请前往 Dify 后台管理': 'To manage Dify knowledge bases, go to the Dify admin console',

  // KnowledgeBaseManager — 新建/编辑 Modal
  '新建公共知识库': 'New Public Knowledge Base',
  '描述该知识库的内容范围，有助于智能体判断何时检索此库':
    'Describe the content scope of this knowledge base to help the agent decide when to retrieve from it',
  'AI 生成简介': 'AI-generate description',
  '已生成简介': 'Description generated',
  '生成失败': 'Generation failed',
  '已更新知识库': 'Knowledge base updated',
  '已创建公共知识库': 'Public knowledge base created',
  '默认分块方法': 'Default Chunk Method',
  '上传文档时的默认分块方法，可在上传时逐文件覆盖': 'Default method used when uploading documents; can be overridden per file at upload time',
  '分块方法在创建后不可更改；如需调整，可在文档级别重建索引时指定。':
    'The chunk method cannot be changed after creation. To adjust, specify a different method when rebuilding the index at the document level.',

  // KnowledgeBaseManager — DocumentsDrawer
  '文档管理': 'Document Management',
  '加载文档失败': 'Failed to load documents',
  '重建索引已启动': 'Re-index started',
  '重建索引失败': 'Failed to re-index',
  '已删除文档': 'Document deleted',
  '标题': 'Title',
  '索引状态': 'Index Status',
  '查看原文': 'View Source',
  '查看分块': 'View Chunks',
  '重建索引': 'Re-index',
  '将删除该文档及其分块与向量数据，不可恢复。': 'This will permanently delete the document along with its chunks and vector data. This action cannot be undone.',

  // KnowledgeBaseManager — UploadModal
  '请先选择文件': 'Please select a file first',
  '成功上传 {n} 个文件，正在后台索引': 'Successfully uploaded {n} file(s), indexing in the background',
  '开始上传': 'Start Upload',
  '文件（支持 PDF / Word / TXT / Markdown / Excel，单文件 ≤ 100MB）':
    'File (PDF / Word / TXT / Markdown / Excel, ≤ 100 MB per file)',
  '点击或拖拽文件到此处上传': 'Click or drag files here to upload',
  '可一次选择多个文件，逐个建立索引': 'You can select multiple files at once; each will be indexed individually',

  // KnowledgeBaseManager — FilePreviewModal
  '原文预览': 'Source Preview',
  '下载原文': 'Download Source',
  '正在转换预览…': 'Converting preview…',
  '加载原文中…': 'Loading source…',
  '无法预览原文': 'Unable to preview source',
  '可点击下方「下载原文」后用本地软件打开。': 'Click "Download Source" below to open it with a local application.',
  '该文件类型暂不支持在线预览': 'This file type is not supported for online preview',
  '请点击下方「下载原文」后用本地软件打开。': 'Click "Download Source" below to open it with a local application.',
  '下载失败': 'Download failed',

  // KnowledgeBaseManager — ChunksDrawer
  '分块管理': 'Chunk Management',
  '共 {n} 个分块': '{n} chunks total',
  '该文档暂无分块（可能仍在索引中）': 'No chunks yet (the document may still be indexing)',
  '暂无标签与问题': 'No tags or questions',
  '编辑分块': 'Edit Chunk',
  '分块内容（修改后会重新向量化）': 'Chunk content (will be re-vectorized after editing)',
  '分块正文内容': 'Chunk text content',
  '标签（用于关键词检索增强）': 'Tags (for keyword retrieval boosting)',
  '输入后回车添加': 'Press Enter to add',
  '检索问题（多面向索引，命中更准）': 'Retrieval questions (multi-angle indexing for better recall)',
  '删除分块': 'Delete Chunk',
  '将删除该分块及其向量数据，不可恢复。': 'This will permanently delete the chunk and its vector data. This action cannot be undone.',
  '已更新分块': 'Chunk updated',
  '分块内容不能为空': 'Chunk content cannot be empty',
  '分块已删除': 'Chunk deleted',
  '请先填写知识库名称': 'Please fill in the knowledge base name first',
  '{n} 个文档索引中，列表将自动刷新': '{n} document(s) indexing; the list refreshes automatically',
  '授权可见团队': 'Authorized teams',
  '选择可见的团队（可留空，稍后再授权）': 'Select teams that can see it (optional; can grant later)',
  '知识库默认对所有人不可见；在此选择的团队创建后即可见。也可稍后在「用户管理 / 团队管理」调整授权。': 'A knowledge base is hidden from everyone by default; teams selected here can see it once created. You can also adjust grants later in User / Team Management.',
  '设置失败：{msg}': 'Update failed: {msg}',
  '「指定可见」库仅对被授权的用户/团队可见、智能体也仅对其检索；授权在「用户管理 / 团队管理」中分配': 'A "Restricted" base is visible only to authorized users/teams and the agent retrieves only those; grants are assigned in User / Team Management.',
};
