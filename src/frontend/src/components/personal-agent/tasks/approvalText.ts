import { t } from '../../../i18n';

const SESSION_OPERATIONS: Record<string, string> = { 'chat.create': '新建会话', 'chat.update': '修改会话', 'chat.delete': '删除会话', 'chat.send': '向会话发送消息', 'run.cancel': '停止运行', 'run.steer': '追加指令' };
// The same words as the backend's IM notice (personal_agent/channels/im_actions.py).
const COMPUTER_ACTIONS: Record<string, string> = { click: '点击', double_click: '双击', right_click: '右键点击', move: '移动鼠标', type: '输入文字', set_value: '填写', key: '按键', hotkey: '按组合键', scroll: '滚动', open_browser: '打开网页', menu: '选择菜单' };
const SCROLL_WAYS: Record<string, string> = { up: '向上滚动', down: '向下滚动', left: '向左滚动', right: '向右滚动' };
/** Typed text a step shows; the backend's detail body carries all of it. */
export const STEP_TEXT_CHARS = 80;

function clip(value: unknown, limit = STEP_TEXT_CHARS): string {
  const text = String(value).split(/\s+/).filter(Boolean).join(' ');
  return text.length <= limit ? text : text.slice(0, limit) + '…';
}

/** The steps of a computer action: a computer.steps action lists them, an old computer.action is one step. */
export function computerSteps(value: Record<string, unknown>): Record<string, unknown>[] {
  if (value.kind === 'computer.steps') return Array.isArray(value.steps) ? value.steps.filter((step): step is Record<string, unknown> => !!step && typeof step === 'object') : [];
  const action = value.action;
  return action && typeof action === 'object' ? [action as Record<string, unknown>] : [];
}

/** One step in words: what it does, on which element or point, with which key, text, address or menu. */
export function describeStep(step: Record<string, unknown>, targets?: Record<string, unknown>): string {
  const kind = String(step.type || '');
  let label = t(COMPUTER_ACTIONS[kind] || '电脑操作');
  if (kind === 'scroll') {
    const amount = typeof step.amount === 'number' ? step.amount : undefined;
    // Without a direction (pixels mode) the sign decides: positive scrolls up, as the drivers do.
    const way = SCROLL_WAYS[String(step.direction)] || (amount === undefined ? '滚动' : amount > 0 ? '向上滚动' : '向下滚动');
    label = amount ? t('{way} {n} 格', { way: t(way), n: Math.abs(amount) }) : t(way);
  }
  const parts: string[] = [];
  // The element as the read named it ("按钮「发送」"); its row number when the approval carries no name.
  const named = step.element && targets ? targets[String(step.element)] : undefined;
  if (typeof named === 'string' && named) parts.push(named);
  else if (step.element) parts.push(t('元素 #{n}', { n: String(step.element).split(':').pop() || '' }));
  else if (step.x !== undefined && step.y !== undefined) parts.push(t('坐标 ({x}, {y})', { x: String(step.x), y: String(step.y) }));
  if (step.text !== undefined && step.text !== null) parts.push('「' + clip(step.text) + '」');
  if (kind === 'key' && step.key) parts.push(String(step.key));
  if (kind === 'hotkey' && Array.isArray(step.keys)) parts.push(step.keys.map(String).join('+'));
  if (step.url) parts.push(clip(step.url, 200));
  if (kind === 'menu' && Array.isArray(step.path)) parts.push('「' + step.path.map(String).join(' > ') + '」');
  // A quoted part follows its word directly, as in the backend's notice: 输入文字「…」.
  return label + parts.map(part => (part.startsWith('「') ? part : ' ' + part)).join('');
}

/** The computer an action runs on and its application / window, as far as the action itself says. */
function computerPlace(value: Record<string, unknown>): string {
  const where = value.target === 'cloud' ? t('你的云电脑') : t('你的电脑');
  const app = value.app ? clip(value.app, 60) : '';
  const title = value.title ? clip(value.title, 60) : '';
  const window = app && title ? app + ' · ' + title : app || title || (value.window === 'screen' ? t('整个屏幕') : '');
  return [where, window].filter(Boolean).join(' · ');
}

/**
 * One operation label and one line of detail, the same shape as the HugAgentOS confirmation bar.
 * A computer action also lists every step.
 */
export function describeApproval(value: Record<string, unknown>): { op: string; detail: string; preview?: string; steps?: string[] } {
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
  const steps = computerSteps(value);
  const targets = value.targets && typeof value.targets === 'object' ? value.targets as Record<string, unknown> : undefined;
  if (steps.length === 1) return { op: t(COMPUTER_ACTIONS[String(steps[0].type)] || '电脑操作'), detail: [computerPlace(value), describeStep(steps[0], targets)].join(' · ') };
  return { op: t('电脑操作（共 {n} 步）', { n: steps.length }), detail: computerPlace(value), steps: steps.map((step, index) => t('第 {n} 步：{step}', { n: index + 1, step: describeStep(step, targets) })) };
}
