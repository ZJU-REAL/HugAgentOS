import { t } from '../../../i18n';

const SESSION_OPERATIONS: Record<string, string> = { 'chat.create': '新建会话', 'chat.update': '修改会话', 'chat.delete': '删除会话', 'chat.send': '向会话发送消息', 'run.cancel': '停止运行', 'run.steer': '追加指令' };
const COMPUTER_ACTIONS: Record<string, string> = { click: '点击', double_click: '双击', right_click: '右键点击', move: '移动鼠标', type: '输入文字', key: '按键', scroll: '滚动', open_browser: '打开网页' };

/** One operation label and one line of detail, the same shape as the HugAgentOS confirmation bar. */
export function describeApproval(value: Record<string, unknown>): { op: string; detail: string; preview?: string } {
  const where = value.target === 'cloud' ? t('云端') : t('本机');
  if (value.kind === 'hugagent.session') {
    const args = (value.args || {}) as Record<string, unknown>;
    const subject = [args.title, args.message].filter(Boolean).map(String).join(' · ');
    return { op: t(SESSION_OPERATIONS[String(value.operation)] || 'HugAgentOS 会话操作'), detail: [where, subject].filter(Boolean).join(' · ') };
  }
  if (value.kind === 'email.send') {
    const files = Array.isArray(value.attachments) ? value.attachments.length : 0;
    return { op: t('发送邮件'), detail: [t('收件人：{to}', { to: String(value.to || '') }), String(value.subject || ''), files ? t('{n} 个附件', { n: files }) : ''].filter(Boolean).join(' · '), preview: String(value.body || '') };
  }
  const action = (value.action || {}) as Record<string, unknown>;
  const target = ['text', 'url', 'key', 'amount'].map(key => action[key]).find(item => item !== undefined);
  const position = action.x !== undefined ? `(${String(action.x)}, ${String(action.y)})` : '';
  return { op: t(COMPUTER_ACTIONS[String(action.type)] || '电脑操作'), detail: [where, target === undefined ? '' : String(target), position].filter(Boolean).join(' · ') };
}
