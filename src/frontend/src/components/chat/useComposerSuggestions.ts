import { useEffect, useMemo, useState } from 'react';
import { listReferencableChats } from '../../api';
import type { ReferencableChat } from '../../types';
import { useAgentMention, type MentionLauncherAction } from '../agent';
import type { SlashEntry } from './SkillSlashPopup';
import { useSkillSlash } from './useComposerSlash';
import { t } from '../../i18n';
import { PROJECT_INIT_COMMAND_ID, PROJECT_INIT_COMMAND_MESSAGE, type ChatCommand } from '../../utils/projectCommands';
import type { ComposerState } from './useComposerState';
import type { ComposerOptions } from './composerTypes';

export function useComposerSuggestions(state: ComposerState, {
  projectComposer, disableMention, activeMode,
}: ComposerOptions) {
  const {
    input, installedPlugins, skills, canInitProject, referencedChats,
    activeLocalMode, planModeAllowed, batchRunnerAllowed, planMode, batchModeOn,
    workflowModeOn, showLoopEntry, loopMode, _currentChat, currentChatId,
  } = state;
  // `/` 面板里的「引用会话」候选。按当前项目范围从后端取，只含标题级信息。
  const [referencableChats, setReferencableChats] = useState<ReferencableChat[]>([]);

  // `/` lists every installed/access-authorized plugin and skill. A personal
  // capability switch only controls default assembly. An off skill is attached
  // to this turn; an explicitly loaded plugin stays expanded for this chat.
  const slashEntries = useMemo<SlashEntry[]>(
    () => {
      const query = input.startsWith('/') ? input.slice(1).toLowerCase() : '';
      const pluginEntries: SlashEntry[] = installedPlugins
        .filter((plugin) => (
          plugin.callable !== false
          && (
            !query
            || plugin.name.toLowerCase().includes(query)
            || plugin.description.toLowerCase().includes(query)
          )
        ))
        .map((plugin) => ({
          kind: 'plugin', id: plugin.install_id, name: plugin.name,
          description: [
            plugin.enabled === false ? t('未启用，调用后本会话保持加载') : '',
            plugin.description.trim(),
          ].filter(Boolean).join(' · '),
          plugin,
        }));
      const skillEntries: SlashEntry[] = (skills || [])
        .filter((skill) => (
          !query || skill.name.toLowerCase().includes(query) || skill.desc.toLowerCase().includes(query)
        ))
        .map((skill) => ({
          kind: 'skill', id: skill.id, name: skill.name,
          description: [
            skill.enabled ? '' : t('未启用，调用后本会话保持加载'),
            skill.desc.trim(),
          ].filter(Boolean).join(' · '),
        }));
      const initCommand: ChatCommand = {
        id: PROJECT_INIT_COMMAND_ID,
        label: t('初始化指令'),
        message: PROJECT_INIT_COMMAND_MESSAGE,
      };
      const commands: SlashEntry[] = canInitProject && ['init', '初始化指令', 'agents.md']
        .some((alias) => alias.includes(query.trim()))
        ? [{ kind: 'command', id: initCommand.id, name: initCommand.message,
            description: t('初始化指令：检查项目并创建或完善 AGENTS.md'),
            command: initCommand }]
        : [];
      const actions: SlashEntry[] = !projectComposer && currentChatId
        && ['fork', '创建聊天分支'].some((alias) => alias.includes(query.trim()))
        ? [{ kind: 'chat_action', action: 'fork', id: 'fork', name: t('创建聊天分支'),
            description: t('为此聊天创建分支') }]
        : [];
      // 已经引用过的不再出现在候选里，避免选两次只生效一次看着像没反应。
      const referenced = new Set(referencedChats.map((c) => c.chat_id));
      const chatEntries: SlashEntry[] = referencableChats
        .filter((chat) => !referenced.has(chat.chat_id))
        .map((chat) => ({
          kind: 'chat', id: chat.chat_id, name: chat.title,
          description: t('{count} 条 · {time}', {
            count: String(chat.message_count ?? 0),
            time: chat.last_active_display,
          }),
          chat,
        }));
      return [...actions, ...commands, ...pluginEntries, ...skillEntries, ...chatEntries];
    },
    [input, installedPlugins, skills, canInitProject, referencableChats, referencedChats, projectComposer, currentChatId],
  );

  // `@` is a launcher first and an agent search second: an empty query shows the
  // high-level capabilities, while typing after it keeps the familiar direct agent search.
  const mentionActions = useMemo<MentionLauncherAction[]>(() => [
    {
      id: 'files' as const,
      name: t('文件和文件夹'),
      description: activeLocalMode
        ? t('从本机选择一个或多个文件')
        : t('从我的空间选择文件，按文件夹浏览'),
    },
    ...(!disableMention ? [{
      id: 'agents' as const,
      name: t('智能体'),
      description: t('选择一个智能体直接处理本轮任务'),
    }] : []),
    ...(planModeAllowed ? [{
      id: 'plan' as const,
      name: t('计划模式'),
      description: t('计划模式：AI 将自动分解任务为多步骤并逐步执行'),
      active: projectComposer ? activeMode === 'plan' : planMode,
    }] : []),
    ...(batchRunnerAllowed ? [{
      id: 'batch' as const,
      name: t('批量执行'),
      description: t('批量执行模式：描述要批量处理的对象与任务，AI 会自动生成可确认的执行计划'),
      active: projectComposer ? activeMode === 'batch' : batchModeOn,
    }] : []),
    {
      id: 'workflow' as const,
      name: t('工作流模式'),
      description: t('工作流模式：面对成百上千个同类工作项时，AI 会写一段作业脚本交给后台并发处理，进度记在台账上，中断可续跑'),
      active: projectComposer ? activeMode === 'workflow' : workflowModeOn,
    },
    ...(showLoopEntry ? [{
      id: 'loop' as const,
      name: t('自主循环'),
      description: t('自主循环：描述一个可验证目标，AI 会反复迭代、自我修正，达标或触预算即停'),
      active: loopMode,
    }] : []),
  ], [
    activeLocalMode,
    disableMention,
    planModeAllowed,
    batchRunnerAllowed,
    projectComposer,
    activeMode,
    planMode,
    batchModeOn,
    workflowModeOn,
    showLoopEntry,
    loopMode,
  ]);

  const {
    mentionVisible, setMentionVisible,
    selectedIndex: mIdx, setSelectedIndex: setMIdx,
    handleInputChange: mentionInputChange, handleKeyDown: mentionKeyDown,
    screen: mentionScreen, candidates: mentionCandidates,
    showAgentPicker: showMentionAgentPicker, backToRoot: backToMentionRoot,
  } = useAgentMention(input, mentionActions);
  const {
    slashVisible, setSlashVisible,
    selectedIndex: sIdx, setSelectedIndex: setSIdx,
    handleSlashInputChange: slashInputChange, handleSlashKeyDown: slashKeyDown,
  } = useSkillSlash();

  // 只在 `/` 面板打开时去取可引用会话，并跟着关键词走：不打开面板就一次请求都不发。
  useEffect(() => {
    if (!slashVisible) return;
    const query = input.startsWith('/') ? input.slice(1).trim() : '';
    let cancelled = false;
    const timer = window.setTimeout(() => {
      void listReferencableChats({
        q: query,
        projectId: _currentChat?.projectId,
        excludeChatId: currentChatId,
      })
        .then((items) => { if (!cancelled) setReferencableChats(items); })
        .catch(() => { if (!cancelled) setReferencableChats([]); });
    }, query ? 200 : 0);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [slashVisible, input, _currentChat?.projectId, currentChatId]);


  return {
    slashEntries, mentionVisible, setMentionVisible, mIdx, setMIdx,
    mentionInputChange, mentionKeyDown, mentionScreen, mentionCandidates,
    showMentionAgentPicker, backToMentionRoot, slashVisible, setSlashVisible,
    sIdx, setSIdx, slashInputChange, slashKeyDown,
  };
}

export type ComposerSuggestions = ReturnType<typeof useComposerSuggestions>;
