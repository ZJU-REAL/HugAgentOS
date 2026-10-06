import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { message } from 'antd';
import { useChatStore } from '../../stores';
import { readComposer, useComposerStore, type ComposerDraft } from '../../stores/composerStore';
import { snapshotComposer, restoreComposer, sameDocument } from './composerDocument';
import type { UserAgentItem } from '../../stores/agentStore';
import type { InstalledPluginItem, ReferencableChat } from '../../types';
import type { MentionCandidate } from '../agent';
import type { SlashEntry } from './SkillSlashPopup';
import { useStableCallback } from '../../hooks/useStableCallback';
import { useComposerCaretScroll } from '../../hooks/useComposerCaretScroll';
import { useChatFork } from '../../hooks/useChatFork';
import { classifyForkCommand } from '../../utils/chatForkCommands';
import { composeCommandMessage, isProjectInitCommand, type ChatCommand } from '../../utils/projectCommands';
import { createRichEditor, disposeRichEditor, richEditor } from './composerRichText';
import '../../styles/composer-rich.css';
import { t } from '../../i18n';
import {
  moveCaretToEnd, removeChipsOfType,
  insertChipAtCursor, removeQueryAtCursor, resetQueryAtCursor, CHAT_CHIP_ICON,
} from './composerEditorDom';
import type { ComposerState } from './useComposerState';
import type { ComposerSuggestions } from './useComposerSuggestions';
import type { InputAreaProps } from './composerTypes';

export function useComposerEditor(state: ComposerState, suggestions: ComposerSuggestions, {
  inputRef, fileInputRef, send, projectComposer,
}: Pick<InputAreaProps, 'inputRef' | 'fileInputRef' | 'send' | 'projectComposer'>) {
  const {
    setActiveMention, setActiveSkill, setActivePlugin, setActiveConnector, setActiveCommand,
    activeLocalMode, draftKey,
    setMySpaceImportOpen, setLoopMode, onEnterMode, canInitProject, addReferencedChat, currentChatId,
  } = state;
  const {
    mentionInputChange, slashInputChange, setMentionVisible, showMentionAgentPicker, setSlashVisible,
  } = suggestions;
  const onMentionInput = useStableCallback(mentionInputChange);
  const onSlashInput = useStableCallback(slashInputChange);
  const { forkChat, pending: forkPending } = useChatFork(currentChatId);
  const editorHostRef = useRef<HTMLDivElement>(null);
  const editorRef = useRef<HTMLDivElement>(null);
  const currentUserId = useChatStore(s => s.currentUserId);
  const epoch = useComposerStore(s => s.epoch);
  const contextKey = currentUserId + ':' + epoch + ':' + draftKey;
  useComposerCaretScroll(editorRef, contextKey);
  const composingRef = useRef(false);
  const [composition, setComposition] = useState({ contextKey, active: false });
  const isComposing = composition.contextKey === contextKey && composition.active;
  const setIsComposing = (active: boolean) => setComposition({ contextKey, active });
  const publishRef = useRef<{ key: string; flush: () => void }>({ key: '', flush: () => {} });
  const restoreRef = useRef<(draft: ComposerDraft) => void>(() => {});
  const seenDraftRef = useRef<ReturnType<typeof readComposer> | null>(null);

  useLayoutEffect(() => {
    if (!editorHostRef.current) return;
    composingRef.current = false;
    let frame = 0;
    let restoring = true;
    let dirty = false;
    const actions = readComposer(draftKey);
    let suggestedInput = actions.input;
    const rich = createRichEditor(editorHostRef.current, () => {
      if (restoring) return;
      dirty = true;
      publish();
    });
    const publish = () => {
      if (rich.isDestroyed || !dirty) return;
      const before = readComposer(draftKey);
      const seen = seenDraftRef.current;
      // External prompt replacement or send consumption wins over a departing editor.
      if (seen?.key === draftKey && (before.input !== seen.input || before.document !== seen.document)) return;
      const next = snapshotComposer(rich);
      if (sameDocument(before.document, next.document ?? null)) return;
      dirty = false;
      // Publish ownership and document synchronously: an upload completion in this same
      // frame must observe any newer edit before consuming its captured turn.
      seenDraftRef.current = { ...before, ...next };
      actions.patch(next);
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        frame = 0;
        if (!composingRef.current && !rich.isDestroyed && !rich.isActive('codeBlock') && !rich.isActive('code')) {
          onMentionInput(next.input ?? '', suggestedInput);
          onSlashInput(next.input ?? '', suggestedInput);
          suggestedInput = next.input ?? '';
        }
      });
    };
    const flush = () => {
      cancelAnimationFrame(frame); frame = 0;
      publish();
    };
    publishRef.current = { key: draftKey, flush };
    editorRef.current = rich.view.dom as HTMLDivElement;
    restoreRef.current = draft => {
      restoring = true;
      cancelAnimationFrame(frame); frame = 0; dirty = false;
      restoreComposer(rich, draft);
      suggestedInput = draft.input;
      restoring = false;
      seenDraftRef.current = readComposer(draftKey);
    };
    restoreRef.current(actions);
    const element = rich.view.dom;
    element.addEventListener('blur', flush);
    return () => {
      // The captured editor, owner and epoch remain authoritative during navigation and IME.
      flush();
      element.removeEventListener('blur', flush);
      if (editorRef.current === element) editorRef.current = null;
      composingRef.current = false;
      disposeRichEditor(rich);
    };
  // The editor's lifetime follows its draft, not the current conversation's metadata.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [contextKey]);

  function syncText() {
    // An old composition-end microtask must never flush the new editor.
    if (useComposerStore.getState().epoch === epoch
        && publishRef.current.key === draftKey) publishRef.current.flush();
  }

  useEffect(() => {
    const element = editorRef.current;
    const rich = element && richEditor(element);
    if (!rich || composingRef.current) return;
    const draft = readComposer(draftKey);
    const seen = seenDraftRef.current;
    if (seen && draft.input === seen.input && sameDocument(draft.document, seen.document)
        && draft.activeMention === seen.activeMention && draft.activeSkill === seen.activeSkill
        && draft.activePlugin === seen.activePlugin && draft.activeConnector === seen.activeConnector
        && draft.activeCommand === seen.activeCommand && draft.referencedChats === seen.referencedChats) return;
    restoreRef.current(draft);
    if (document.activeElement === element) moveCaretToEnd(element!);
  });

  // ── Expose the editor as inputRef for external .focus() calls ──
  const externalEditorRef = inputRef as React.RefObject<HTMLElement | null>;
  useEffect(() => {
    externalEditorRef.current = editorRef.current;
    return () => { externalEditorRef.current = null; };
  }, [externalEditorRef, contextKey]);

  // ── Chip insertion handlers ──
  /** Insert a sub-agent mention chip and set it as the currently active one (shared by the @ popup and the "+" menu). */
  function applyMention(agent: UserAgentItem) {
    const ed = editorRef.current;
    if (!ed) return;
    insertChipAtCursor(ed, '@', agent.name, 'jx-editorChip--mention', 'mention', agent.agent_id, undefined, { id: agent.agent_id, name: agent.name });
    setActiveMention({ id: agent.agent_id, name: agent.name });
    setMentionVisible(false);
    syncText();
    ed.focus();
  }

  function onMentionSelect(agent: UserAgentItem) {
    const ed = editorRef.current;
    if (!ed) return;
    removeQueryAtCursor(ed, '@');
    applyMention(agent);
  }

  /** Run a first-level `@` launcher action without leaving the typed trigger behind. */
  function onMentionCandidateSelect(candidate: MentionCandidate) {
    if (candidate.kind === 'agent') {
      onMentionSelect(candidate.agent);
      return;
    }

    if (candidate.action.id === 'agents') {
      const ed = editorRef.current;
      if (ed) {
        resetQueryAtCursor(ed, '@');
        syncText();
      }
      showMentionAgentPicker();
      return;
    }

    const ed = editorRef.current;
    if (ed) removeQueryAtCursor(ed, '@');
    setMentionVisible(false);
    syncText();

    if (candidate.action.id === 'files') {
      if (activeLocalMode) fileInputRef.current?.click();
      else setMySpaceImportOpen(true);
      return;
    }
    if (candidate.action.id === 'loop') {
      setLoopMode(true);
      requestAnimationFrame(() => ed?.focus());
      return;
    }

    onEnterMode(candidate.action.id);
    requestAnimationFrame(() => ed?.focus());
  }

  /** Pick a sub-agent from the "+" menu: move the caret to the end first, then insert the chip. */
  function onPickAgentFromMenu(agent: UserAgentItem) {
    const ed = editorRef.current;
    if (!ed) return;
    ed.focus();
    moveCaretToEnd(ed);
    applyMention(agent);
  }

  /** Insert a skill chip and set it as the currently active skill (shared by the / popup and the "+" menu). */
  function applySkill(skillId: string, skillName: string) {
    const ed = editorRef.current;
    if (!ed) return;
    insertChipAtCursor(ed, '/', skillName, 'jx-editorChip--skill', 'skill', skillId, undefined, { id: skillId, name: skillName });
    setActiveSkill({ id: skillId, name: skillName });
    setSlashVisible(false);
    syncText();
    ed.focus();
  }

  function onSlashSelect(skillId: string, skillName: string) {
    const ed = editorRef.current;
    if (!ed) return;
    removeQueryAtCursor(ed, '/');
    applySkill(skillId, skillName);
  }

  /** Pick a skill from the "+" menu: move the caret to the end first, then insert the chip (the editor may not have focus when the menu closes). */
  function onPickSkillFromMenu(skillId: string, skillName: string) {
    const ed = editorRef.current;
    if (!ed) return;
    ed.focus();
    moveCaretToEnd(ed);
    applySkill(skillId, skillName);
  }

  /** Insert a plugin chip and retain its authoritative installation id for server-side expansion. */
  function applyPlugin(p: InstalledPluginItem) {
    const ed = editorRef.current;
    if (!ed) return;
    insertChipAtCursor(ed, '/', p.name, 'jx-editorChip--plugin', 'plugin', p.install_id, undefined, { id: p.install_id, name: p.name });
    setActivePlugin({
      id: p.install_id,
      name: p.name,
    });
    setSlashVisible(false);
    syncText();
    ed.focus();
  }

  function onSlashSelectPlugin(p: InstalledPluginItem) {
    const ed = editorRef.current;
    if (!ed) return;
    removeQueryAtCursor(ed, '/');
    applyPlugin(p);
  }

  function onPickPluginFromMenu(p: InstalledPluginItem) {
    const ed = editorRef.current;
    if (!ed) return;
    ed.focus();
    moveCaretToEnd(ed);
    applyPlugin(p);
  }

  /** Select one connector for this turn and render it as an inline MCP chip. */
  function applyConnector(connectorId: string, connectorName: string) {
    const ed = editorRef.current;
    if (!ed) return;
    // One direct connector can be selected at a time. Replace an existing connector chip
    // instead of leaving the DOM with two chips backed by one store value.
    removeChipsOfType(ed, 'connector');
    insertChipAtCursor(ed, 'MCP', connectorName, 'jx-editorChip--connector', 'connector', connectorId, undefined, { id: connectorId, name: connectorName });
    setActiveConnector({ id: connectorId, name: connectorName });
    syncText();
    ed.focus();
  }

  /** 引用一条斜杠命令：与技能 / 插件 / 连接器完全同一套内联 chip——选中只是把命令放进输入框，
   *  用户再按回车才发出去（原来选中即发，用户来不及看清就已经跑起来了）。 */
  function applyCommand(command: ChatCommand) {
    const ed = editorRef.current;
    if (!ed) return;
    insertChipAtCursor(ed, '/', command.label, 'jx-editorChip--command', 'command', command.id, undefined, command);
    setActiveCommand(command);
    setSlashVisible(false);
    syncText();
    ed.focus();
  }

  function onSlashSelectCommand(command: ChatCommand) {
    const ed = editorRef.current;
    if (!ed) return;
    removeQueryAtCursor(ed, '/');
    applyCommand(command);
  }

  function onPickConnectorFromMenu(connectorId: string, connectorName: string) {
    const ed = editorRef.current;
    if (!ed) return;
    ed.focus();
    moveCaretToEnd(ed);
    applyConnector(connectorId, connectorName);
  }

  async function runFork() {
    if (projectComposer) {
      void message.warning(t('请在已有聊天中创建分支'));
      return;
    }
    await forkChat(undefined, { clearInput: readComposer(draftKey).input });
  }

  function sendFromComposer() {
    syncText();
    const { input: composerText, activeCommand: pendingCommand } = readComposer(draftKey);
    const forkCommand = classifyForkCommand(composerText);
    if (forkCommand) {
      if (forkCommand === 'invalid') {
        void message.warning(t('用法：/fork（不支持参数）'));
      } else {
        void runFork();
      }
      return;
    }
    const value = composeCommandMessage(composerText, pendingCommand);
    if (isProjectInitCommand(value) && !canInitProject) {
      void message.warning(t('请在有编辑权限的具体项目中使用普通对话，并移除已选择的能力后初始化指令'));
      return;
    }
    send();
  }

  /** 引用一段历史会话：与技能 / 插件 / 连接器 / @智能体 完全同一套内联 chip，
   *  只是换个前缀和配色。多条引用靠 chip 上的 chatId 与 store 对齐。 */
  function applyChatReference(chat: ReferencableChat) {
    const ed = editorRef.current;
    if (!ed) return;
    insertChipAtCursor(ed, '', chat.title, 'jx-editorChip--chat', 'chat', chat.chat_id, CHAT_CHIP_ICON, chat);
    addReferencedChat(chat);
    syncText();
  }

  function onSlashSelectChat(chat: ReferencableChat) {
    const ed = editorRef.current;
    if (!ed) return;
    removeQueryAtCursor(ed, '/');
    applyChatReference(chat);
    setSlashVisible(false);
  }

  function onSlashEntrySelect(entry: SlashEntry) {
    if (entry.kind === 'chat_action') {
      setSlashVisible(false);
      void runFork();
      return;
    }
    if (entry.kind === 'chat') {
      onSlashSelectChat(entry.chat);
      return;
    }
    if (entry.kind === 'command') {
      if (!canInitProject) return;
      onSlashSelectCommand(entry.command);
      return;
    }
    if (entry.kind === 'plugin') {
      onSlashSelectPlugin(entry.plugin);
      return;
    }
    onSlashSelect(entry.id, entry.name);
  }


  return {
    editorHostRef, editorRef, composingRef, isComposing, setIsComposing, syncText,
    onMentionCandidateSelect, onPickAgentFromMenu, onPickSkillFromMenu,
    onPickPluginFromMenu, onPickConnectorFromMenu, sendFromComposer,
    applyChatReference, onSlashEntrySelect, forkPending,
  };
}

export type ComposerEditor = ReturnType<typeof useComposerEditor>;
