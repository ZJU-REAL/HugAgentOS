/** 英文字典（stores 域）：key 为中文原文，value 为英文译文。 */
export const STORES_DICT: Record<string, string> = {
  '调用智能体': 'Call Agent',
  // settingsStore — memory messages
  '记忆设置更新失败': 'Failed to update memory settings',
  '写入记忆设置更新失败': 'Failed to update memory write settings',
  '重排序设置更新失败': 'Failed to update reranker settings',
  '本体校验设置更新失败': 'Failed to update ontology validation settings',
  '加载长期记忆失败': 'Failed to load long-term memory',
  '加载档案记忆失败': 'Failed to load profile memory',
  '加载图谱记忆失败': 'Failed to load graph memory',
  '删除记忆失败': 'Failed to delete memory',
  '已清除所有记忆': 'All memories cleared',
  '清除记忆失败': 'Failed to clear memories',

  // automationChatStore / mySpaceStore / history — automation display

  // mySpaceStore — error
  '当前作用域不支持上传': 'Upload is not supported in the current scope',


  // projectStore — error

  // pageConfigStore — builtin app names and descriptions
  '描述复杂任务，AI 自动分解为多步骤并逐步执行，适用于数据分析、报告生成、政策解读等场景':
    'Describe a complex task and the AI will break it into steps and execute them, ideal for data analysis, report generation, and policy interpretation.',
  '设置定时或周期性 AI 任务，支持自然语言提示词和计划模式的自动执行，适用于定期报告、数据监控等场景':
    'Set up scheduled or recurring AI tasks with natural-language prompts or plan mode, ideal for periodic reports and data monitoring.',
  // homepage shortcut labels / capability cards
  '知识检索': 'Knowledge Search',
  '数据分析': 'Data Analysis',
  // default app config
  '企业调研': 'Company Research',

  // chatStore — default chat title

  // constants — tool name overrides
  '读取文件': 'Read File',
  '加载技能': 'Load Skill',
  '加载插件': 'Load Plugin',

  // roles — display labels
  '无权限': 'No Access',

  // confirmDelete
  '确定要删除{kind}「{name}」吗？此操作不可撤销。': 'Are you sure you want to delete {kind}"{name}"? This action cannot be undone.',

  // export
  '（该对话暂无消息）': '(No messages in this chat)',
  '助手': 'Assistant',

  // fileParser

  // citations
};
