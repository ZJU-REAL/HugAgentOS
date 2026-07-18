/** 英文字典（adminMisc 域）：key 为中文原文，value 为英文译文。 */
export const ADMIN_MISC_DICT: Record<string, string> = {
  // ── UpdatesEditor ──
  '已导出': 'Exported',
  '已导入 {n} 条记录': 'Imported {n} records',
  'JSON 解析失败': 'JSON parse error',
  '功能更新已保存': 'Updates saved',
  '共 {n} 条记录': '{n} records',
  '新增条目': 'New Entry',
  '保存并发布': 'Save & Publish',
  '日期': 'Date',
  '新增功能更新': 'New Update',
  '编辑功能更新': 'Edit Update',
  '日期（如 02.27）': 'Date (e.g. 02.27)',
  '年份': 'Year',
  // UpdateCategory display labels (value stays Chinese, label uses t())
  '模型迭代': 'Model Update',
  '信息处理': 'Data Processing',
  '应用上新': 'New Feature',
  '体验优化': 'UX Improvement',

  // ── PromptHubEditor ──
  '已导入 {n} 条提示词': 'Imported {n} prompts',
  '提示词中心已保存': 'Prompt Hub saved',
  '共 {n} 条提示词': '{n} prompts',
  '新增提示词': 'New Prompt',
  '提示词内容': 'Prompt Content',
  '编辑提示词': 'Edit Prompt',
  '请输入标题': 'Please enter a title',
  '请输入提示词内容': 'Please enter prompt content',

  // ── CapsEditor ──
  '已导入 {n} 项能力': 'Imported {n} capabilities',
  '能力中心已保存': 'Ability Center saved',
  '共 {n} 项能力': '{n} capabilities',
  '新增能力': 'New Capability',
  '新增能力项': 'New Capability',
  '编辑能力项': 'Edit Capability',
  '子项数': 'Sub-items',
  '子项（每行一条）': 'Sub-items (one per line)',

  // ── ServiceConfigsEditor ──
  '服务配置已保存': 'Service config saved',
  '连接失败：{msg}': 'Connection failed: {msg}',
  '服务配置已导出': 'Service config exported',
  '服务配置已导入': 'Service config imported',
  '保存所有配置': 'Save All',
  '数据库查询服务': 'Database Query Service',
  '知识库服务': 'Knowledge Base Service',
  '产业知识中心': 'Industry Knowledge Center',
  '文件解析服务': 'File Parsing Service',
  '互联网搜索': 'Internet Search',
  '钉钉工作台 (Custom App)': 'DingTalk Workspace (Custom App)',
  '飞书工作台 (Custom App)': 'Feishu Workspace (Custom App)',
  '初始化飞书应用': 'Initialize Feishu app',
  '重新初始化': 'Re-initialize',
  '飞书应用已配置': 'Feishu app configured',
  '飞书应用配置二维码': 'Feishu app setup QR code',
  '一键初始化全组共用的飞书应用，无需手动创建应用或填写凭据。': 'One-click setup of the org-wide shared Feishu app - no need to manually create an app or enter credentials.',
  '用管理员的飞书 App 扫码完成应用配置（仅需一次）。': 'Scan with the admin\'s Feishu app to complete app setup (one-time only).',
  '或点此在浏览器中打开配置页': 'Or open the setup page in a browser',
  '完成后自动就绪，无需手动刷新。': 'It will be ready automatically once done - no manual refresh needed.',
  '全组共用此应用，用户在「设置 → 集成」扫码登录各自账号即可。': 'The whole org shares this app; users just scan to log in with their own account under "Settings - Integrations".',
  '初始化失败：{msg}': 'Initialization failed: {msg}',
  '百度搜索': 'Baidu Search',
  '选择搜索引擎': 'Select search engine',
  '选择知识库后端': 'Select KB backend',
  '选择解析后端': 'Select parsing backend',
  '选择解析方法': 'Select parse method',
  '逗号分隔的数据集 ID，为空则全部允许': 'Comma-separated dataset IDs; leave empty to allow all',
  '输入{name}': 'Enter {name}',

  // ── ManualEditor ──
  '加载操作手册信息失败': 'Failed to load manual info',
  '仅支持 PDF 文件': 'Only PDF files are supported',
  '文件大小不能超过 50MB': 'File size must not exceed 50MB',
  '操作手册上传成功': 'Manual uploaded successfully',
  '上传于 {dt}': 'Uploaded at {dt}',
  '上传中...': 'Uploading...',
  '点击或拖拽 PDF 文件到此处上传': 'Click or drag a PDF file here to upload',
  '仅支持 PDF 格式，大小不超过 50MB。上传后将替换现有操作手册。': 'PDF only, max 50 MB. Uploading will replace the existing manual.',

  // ── IconPicker ──
  '仅支持 PNG / JPG / SVG / WebP': 'Only PNG / JPG / SVG / WebP are supported',
  '图片大小不能超过 2MB': 'Image size must not exceed 2 MB',
  '图标上传成功': 'Icon uploaded',
  '上传失败：{err}': 'Upload failed: {err}',
  '从图标库选择': 'Pick from Library',
  '上传图标': 'Upload Icon',
  '移除图标': 'Remove Icon',
  '应用图标库': 'App Icon Library',

  // ── LoginView ──
  '请输入管理员 Token 以继续': 'Enter the Admin Token to continue',
  'Token 验证失败，请检查 ADMIN_TOKEN 是否正确': 'Token verification failed. Check that ADMIN_TOKEN is correct.',
  '后台管理': 'Admin Console',
  '验证并进入': 'Verify & Enter',

  // ── AdminApp ──
  '管理员登录已失效，请重新输入 ADMIN_TOKEN': 'Admin session expired. Please re-enter the ADMIN_TOKEN.',
  '功能更新': 'Updates',
  '技能管理': 'Skill Management',
  '待审草稿': 'Pending Drafts',
  '沙盒依赖': 'Sandbox Dependencies',
  '知识库管理': 'Knowledge Base',
  '系统配置': 'System Config',

  // ── adminApi.ts ──

  // ── AdminApp ──

  // ── PromptHubEditor placeholders ──
  '例：政策解读（知识库优先）': 'e.g. Policy Interpretation (KB-first)',
  '请基于内部知识库，解读【政策名称】对...': 'Based on the internal knowledge base, interpret [Policy Name] on...',

  // ── CapsEditor placeholder ──
  '单企业精准查询\n多维度筛选\n指标自动换算': 'Single-enterprise precise query\nMulti-dimensional filtering\nAuto metric conversion',
};
