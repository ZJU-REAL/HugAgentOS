/** 英文字典（hooks 域）：key 为中文原文，value 为英文译文。 */
export const HOOKS_DICT: Record<string, string> = {
  // useStreaming — file upload warnings
  '文件"{name}"上传失败，发送后将无法下载': 'File "{name}" upload failed; download will be unavailable after sending',
  // useStreaming — zombie run toast
  '上次会话因服务端重启未完成，请重新发起': 'The previous session was interrupted by a server restart. Please send again.',
  // useStreaming — API config missing
  '请先在设置中配置 API 地址。': 'Please configure the API address in Settings first.',
  // useStreaming — tool name fallbacks (note: '工具调用' also in tool.ts, '附件' also in adminSkills.ts)
  // useStreaming — subagent display name
  '调用子智能体：{name}': 'Calling Sub-agent: {name}',
  // useStreaming — file confirm timeout
  '一项「我的空间」写确认已超时取消，如仍需要请重新发起。': 'A My Space write confirmation has timed out and been cancelled. Please resend if needed.',
  // useStreaming — design pick timeout
  '设计方案选择已超时，助手将自行选择方案继续。': 'The design picker timed out; the assistant will pick an option and continue.',
  // useStreaming — network / send errors
  '与服务端连接中断，请重新发送': 'Connection to server lost. Please resend.',
  '发送失败：{msg}': 'Send failed: {msg}',
  '流式响应异常': 'Streaming response error',
  // useStreaming — regenerate / edit errors
  '重新生成失败：{msg}': 'Regeneration failed: {msg}',
  '编辑重发失败：{msg}': 'Edit & resend failed: {msg}',
  // useStreaming — cancel batch error
  '取消批量并继续失败：{msg}': 'Cancel batch & resume failed: {msg}',
  // usePlanMode — plan card status
  '执行中...': 'Running...',
  '执行出错': 'Execution error',
  // usePlanMode — plan generate stream messages
  '🔍 正在分析任务并生成执行计划...': '🔍 Analyzing task and generating execution plan...',
  '计划生成失败：{error}': 'Plan generation failed: {error}',
  '计划生成未返回有效结果，请重试。': 'Plan generation returned no valid result. Please try again.',
  // usePlanMode — request errors
  '计划执行请求失败: {status}': 'Plan execution request failed: {status}',
  '计划生成请求失败: {status}': 'Plan generation request failed: {status}',
  '计划模式出错：{msg}': 'Plan mode error: {msg}',
  // useChatActions — delete chat modal
  '删除历史对话': 'Delete Chat',
  '确定删除该历史对话吗？该操作不可恢复。': 'Are you sure you want to delete this chat? This action cannot be undone.',
  '删除': 'Delete',
  // useChatActions — favorite sync errors
  '收藏状态同步失败，请重试': 'Failed to sync favorite status. Please try again.',
  '网络异常，收藏状态同步失败': 'Network error — failed to sync favorite status.',
  // useChatActions — export
  '导出失败：加载对话内容失败': 'Export failed: could not load chat content.',
  '对话记录': 'Chat History',
  '对话已导出为 PDF': 'Chat exported as PDF',
  // useChatActions — share errors
  '当前会话不存在': 'Chat session not found.',
};
