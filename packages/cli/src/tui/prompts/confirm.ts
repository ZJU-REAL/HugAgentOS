/** `file_confirm` / `design_pick` answered inline: allow / allow for this session / deny, or one of its options. */
import type { FileDecision } from '../../engine/engine.js';
import type { ConfirmRequest } from '../events.js';
import { TitledSelect } from './select.js';

export interface ConfirmDecision { decision: FileDecision; optionId?: string }

const OP_LABELS: Record<string, string> = {
  write: '写入文件', create: '创建文件', edit: '修改文件', delete: '删除文件', move: '移动文件', rename: '重命名',
  mkdir: '创建目录', exec: '执行命令', run: '执行命令', command: '执行命令', bash: '执行命令', tool: '使用工具',
};

export function confirmTitle(request: ConfirmRequest): string {
  if (request.pickOnly) return '需要你选择';
  const op = OP_LABELS[request.op] ?? (request.op || '操作');
  return `需要确认 · ${op}${request.path ? ' ' + request.path : ''}`;
}

export function confirmItems(request: ConfirmRequest) {
  const choices = request.options.map((o) => ({ value: 'choice:' + o.id, label: o.title, description: o.brief }));
  if (request.pickOnly) return [...choices, { value: 'skip', label: '让助手决定' }];
  return [
    { value: 'allow', label: '允许', description: '只这一次' },
    { value: 'allow_session', label: '本会话内都允许', description: '同类操作不再询问' },
    ...choices,
    { value: 'deny', label: '拒绝', description: '助手会收到拒绝并继续' },
  ];
}

export function toDecision(value: string): ConfirmDecision {
  if (value.startsWith('choice:')) return { decision: 'choice', optionId: value.slice('choice:'.length) };
  return { decision: value as FileDecision };
}

export function createConfirmPrompt(request: ConfirmRequest, done: (d: ConfirmDecision) => void): TitledSelect {
  return new TitledSelect({
    title: '⚠ ' + confirmTitle(request),
    body: request.message,
    items: confirmItems(request),
    attention: true,
    hint: request.pickOnly ? '↑↓ 选择 · enter 确认 · esc 让助手决定' : '↑↓ 选择 · enter 确认 · esc 拒绝',
    onSelect: (item) => done(toDecision(item.value)),
    onCancel: () => done({ decision: request.pickOnly ? 'skip' : 'deny' }),
  });
}
