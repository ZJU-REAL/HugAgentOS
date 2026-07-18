/** 英文字典（core 域）：key 为中文原文，value 为英文译文。 */
export const CORE_DICT: Record<string, string> = {
  // ── 语境消歧条目（tCtx 专用，key 形如 `中文#ctx`） ──
  '关闭#switch': 'Off',
  '退出#leave': 'Leave',
  '恢复#restore': 'Restore',
  '启用#state': 'Enabled',
  '创建#time': 'Created',
  '图片#unit': 'image ',
  'HugAgentOS AI 智能助手': 'HugAgentOS AI Assistant',
  '基于 AI 能力的场景化智能应用': 'AI-powered smart applications for business scenarios',
  '系统托管': 'System-managed',
  // App.tsx — branding / fallbacks
  'HugAgentOS': 'HugAgentOS',
  '新对话': 'New Chat',
  '项目：': 'Project: ',
  '项目': 'Project',
  '对话': 'Chat',
  '计划模式': 'Plan Mode',

  // App.tsx — recommend banner
  '推荐用法：优先使用知识库检索可提升可引用性与结果可靠性。': 'Tip: Using Knowledge Base retrieval improves citability and result reliability.',
  '前往知识库 >': 'Go to Knowledge Base >',
  '关闭': 'Close',

  // api.ts — upload error messages
  '文件过大，单个文件不能超过 {n} MB': 'File too large. Maximum file size is {n} MB.',
  '不支持的文件格式，仅支持：{allowed}': 'Unsupported file format. Allowed types: {allowed}.',
  '不支持的文件格式': 'Unsupported file format.',
  '上传失败 ({status})': 'Upload failed ({status}).',
  '上传失败：{status}': 'Upload failed: {status}.',
  '导出失败：{status}': 'Export failed: {status}.',
  '头像上传失败: {status}': 'Avatar upload failed: {status}.',
};
