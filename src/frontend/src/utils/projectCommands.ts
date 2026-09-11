import { t } from '../i18n';

export const PROJECT_INIT_COMMAND_ID = 'project-init';
/** 发给后端的命令原文（后端 core/services/project_init.py 的 INIT_COMMANDS 与之对应）。 */
export const PROJECT_INIT_COMMAND_MESSAGE = '/init';

/** Shared command recognition for the composer and wire-message decoration. */
export function isProjectInitCommand(text: string): boolean {
  return [PROJECT_INIT_COMMAND_MESSAGE, '/初始化指令'].includes(text.trim());
}

export function canInitializeProject(options: {
  projectId?: string | null;
  permission?: string;
  busy: boolean;
  hasCapability: boolean;
  specialMode: boolean;
}): boolean {
  return !!options.projectId
    && (options.permission === 'admin' || options.permission === 'edit')
    && !options.busy && !options.hasCapability && !options.specialMode;
}

/** 输入框里被引用的一条命令：id 用于面板选中态，label 是 chip 上的文字，message 是发出去的命令原文。 */
export type ChatCommand = { id: string; label: string; message: string };

/** 命令 chip 与技能 / 插件 / 连接器 chip 一样不产生编辑器文本，发送时在这里还原成命令原文。
 *  chip 后面还打了字就照常拼在后面——与手打「/init 再说点别的」完全一致，后端按普通消息处理。 */
export function composeCommandMessage(text: string, command?: { message: string } | null): string {
  const typed = text.trim();
  if (!command) return typed;
  return [command.message, typed].filter(Boolean).join(' ');
}

/** 会话标题取首条消息的前 18 个字——但命令原文当标题等于没标题（历史列表里躺着一条叫
 *  「/init」的对话，不知道是哪个项目、干了什么）。命令开头的会话按命令本身命名。 */
export function seedChatTitle(message: string, fallback: string): string {
  if (isProjectInitCommand(message)) return t('初始化项目指令');
  return message.slice(0, 18) || fallback;
}
