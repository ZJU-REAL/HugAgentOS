import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { message } from 'antd';
import { useChatStore } from '../../stores';
import type { UserAgentItem } from '../../stores/agentStore';
import type { InstalledPluginItem, ReferencableChat } from '../../types';
import type { MentionCandidate } from '../agent';
import type { SlashEntry } from './SkillSlashPopup';
import { useComposerCaretScroll } from '../../hooks/useComposerCaretScroll';
import { useChatFork } from '../../hooks/useChatFork';
import { classifyForkCommand } from '../../utils/chatForkCommands';
import { composeCommandMessage, isProjectInitCommand, type ChatCommand } from '../../utils/projectCommands';
import { t } from '../../i18n';
import {
  getEditorText, setEditorPlainText, moveCaretToEnd, removeChipsOfType, insertChipAtStart,
  insertChipAtCursor, removeQueryAtCursor, resetQueryAtCursor, CHAT_CHIP_ICON,
} from './composerEditorDom';
import type { ComposerState } from './useComposerState';
import type { ComposerSuggestions } from './useComposerSuggestions';
import type { InputAreaProps } from './composerTypes';

export function useComposerEditor(state: ComposerState, suggestions: ComposerSuggestions, {
  inputRef, fileInputRef, send, projectComposer,
}: Pick<InputAreaProps, 'inputRef' | 'fileInputRef' | 'send' | 'projectComposer'>) {
  const {
    input, setInput, activeMention, activeSkill, activePlugin, activeConnector, activeCommand,
    setActiveMention, setActiveSkill, setActivePlugin, setActiveConnector, setActiveCommand,
    referencedChats, clearReferencedChats, _currentChat, isSiteChat, activeLocalMode,
    setMySpaceImportOpen, setLoopMode, onEnterMode, canInitProject, addReferencedChat, currentChatId,
  } = state;
  const {
    mentionInputChange, slashInputChange, setMentionVisible, showMentionAgentPicker, setSlashVisible,
  } = suggestions;
  const { forkChat, pending: forkPending } = useChatFork(currentChatId);
  const editorRef = useRef<HTMLDivElement>(null);
  useComposerCaretScroll(editorRef);
  const composingRef = useRef(false);
  const [isComposing, setIsComposing] = useState(false);
  const prevTextRef = useRef('');

  // ── Sync editor text → store ──
  const syncTextRef = useRef<() => void>(() => {});
  useLayoutEffect(() => {
    syncTextRef.current = () => {
      if (!editorRef.current) return;
      const text = getEditorText(editorRef.current);
      const prev = prevTextRef.current;
      if (text === prev) return; // no change
      prevTextRef.current = text;
      setInput(text);
      mentionInputChange(text, prev);
      slashInputChange(text, prev);
    };
  });
  function syncText() { syncTextRef.current(); }

  // ── Native input event listener (more reliable than React onInput for contentEditable) ──
  useEffect(() => {
    const el = editorRef.current;
    if (!el) return;
    const handler = () => { if (!composingRef.current) syncTextRef.current(); };
    el.addEventListener('input', handler);
    return () => el.removeEventListener('input', handler);
  }, []);

  // ── Sync external store updates back into the contentEditable editor ──
  useEffect(() => {
    const editor = editorRef.current;
    if (!editor || composingRef.current || input === prevTextRef.current) return;

    const hadMentionChip = !!editor.querySelector('[data-chip="mention"]');
    const hadSkillChip = !!editor.querySelector('[data-chip="skill"]');
    const hadPluginChip = !!editor.querySelector('[data-chip="plugin"]');
    const hadConnectorChip = !!editor.querySelector('[data-chip="connector"]');
    const hadCommandChip = !!editor.querySelector('[data-chip="command"]');
    const hadChatChip = !!editor.querySelector('[data-chip="chat"]');

    setEditorPlainText(editor, input);
    prevTextRef.current = input;

    if (hadMentionChip && activeMention) setActiveMention(null);
    if (hadSkillChip && activeSkill) setActiveSkill(null);
    if (hadPluginChip && activePlugin) setActivePlugin(null);
    if (hadConnectorChip && activeConnector) setActiveConnector(null);
    if (hadCommandChip && activeCommand) setActiveCommand(null);
    if (hadChatChip && referencedChats.length > 0) clearReferencedChats();

    if (document.activeElement === editor) {
      moveCaretToEnd(editor);
    }
  }, [
    activeMention, activeSkill, activePlugin, activeConnector, activeCommand, input,
    setActiveMention, setActiveSkill, setActivePlugin, setActiveConnector, setActiveCommand,
    referencedChats, clearReferencedChats,
  ]);

  // Connector chips are per-turn composer state. Clear stale DOM chips after switching chats,
  // panels, users, or starting a new chat, just like the site-plugin safety invariant below.
  useEffect(() => {
    const editor = editorRef.current;
    if (!editor || activeConnector) return;
    if (removeChipsOfType(editor, 'connector')) syncText();
  }, [activeConnector, _currentChat?.id]);

  // 命令 chip 同一条安全网，而且它是唯一出口：只带一个命令 chip 发送时输入框文本前后都是空的，
  // 上面那个「input 变了才重画编辑器」的 effect 根本不会触发，chip 会留在框里。
  useEffect(() => {
    const editor = editorRef.current;
    if (!editor || activeCommand) return;
    if (removeChipsOfType(editor, 'command')) syncText();
  }, [activeCommand, _currentChat?.id]);

  // 会话引用 chip 与 connector chip 同一条安全网：编辑器 DOM 是所有会话共用的一个元素，
  // 切换会话时 store 里的引用已清空，DOM 里的 chip 不会自己消失，得显式扫掉。
  useEffect(() => {
    const editor = editorRef.current;
    if (!editor || referencedChats.length > 0) return;
    if (removeChipsOfType(editor, 'chat')) syncText();
  }, [referencedChats, _currentChat?.id]);

  // ── Plugin-first entry points: render their activated plugin as an inline reference chip ──
  // Site building and scheduled-task creation can enter chat with a plugin already active.
  // The chip must be inserted before any prefilled prompt so the user sees both the referenced
  // plugin and the editable instruction exactly as they would after choosing a plugin manually.
  //
  // Key point (fixes plugin references leaking across chats): the editor DOM is a single
  // element shared by all chats, so plugin chips do not disappear automatically on chat
  // switch. We enforce a **strong invariant** as the safety net — "a plugin chip must
  // correspond to an activePlugin": whenever activePlugin is empty (setCurrentChatId
  // already recomputed it to null when switching to a non-site chat, or the user deleted
  // the chip), remove all stale plugin chips from the editor. It does not depend on any
  // "did we switch" check, so nothing slips through.
  useEffect(() => {
    const editor = editorRef.current;
    if (!editor) return;
    if (!activePlugin) {
      const stale = editor.querySelectorAll('[data-chip="plugin"]');
      if (stale.length) {
        stale.forEach((el) => el.remove());
        syncText();
      }
      return;
    }
    if (!editor.querySelector('[data-chip="plugin"]')) {
      insertChipAtStart(editor, '/', activePlugin.name, 'jx-editorChip--plugin', 'plugin');
      syncText();
    }
  }, [isSiteChat, activePlugin, _currentChat?.id, input]);

  // ── Expose the editor as inputRef for external .focus() calls ──
  const externalEditorRef = inputRef as React.RefObject<HTMLElement | null>;
  useEffect(() => {
    externalEditorRef.current = editorRef.current;
    return () => { externalEditorRef.current = null; };
  }, [externalEditorRef]);

  // ── Chip insertion handlers ──
  /** Insert a sub-agent mention chip and set it as the currently active one (shared by the @ popup and the "+" menu). */
  function applyMention(agent: UserAgentItem) {
    const ed = editorRef.current;
    if (!ed) return;
    insertChipAtCursor(ed, '@', agent.name, 'jx-editorChip--mention');
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
    insertChipAtCursor(ed, '/', skillName, 'jx-editorChip--skill');
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
    insertChipAtCursor(ed, '/', p.name, 'jx-editorChip--plugin', 'plugin');
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
    insertChipAtCursor(ed, 'MCP', connectorName, 'jx-editorChip--connector', 'connector');
    setActiveConnector({ id: connectorId, name: connectorName });
    syncText();
    ed.focus();
  }

  /** 引用一条斜杠命令：与技能 / 插件 / 连接器完全同一套内联 chip——选中只是把命令放进输入框，
   *  用户再按回车才发出去（原来选中即发，用户来不及看清就已经跑起来了）。 */
  function applyCommand(command: ChatCommand) {
    const ed = editorRef.current;
    if (!ed) return;
    insertChipAtCursor(ed, '/', command.label, 'jx-editorChip--command', 'command');
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
    await forkChat(undefined, { clearInput: useChatStore.getState().input });
  }

  function sendFromComposer() {
    const { input: composerText, activeCommand: pendingCommand } = useChatStore.getState();
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
    insertChipAtCursor(ed, '', chat.title, 'jx-editorChip--chat', 'chat', chat.chat_id, CHAT_CHIP_ICON);
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
    editorRef, composingRef, isComposing, setIsComposing, syncText,
    onMentionCandidateSelect, onPickAgentFromMenu, onPickSkillFromMenu,
    onPickPluginFromMenu, onPickConnectorFromMenu, sendFromComposer,
    applyChatReference, onSlashEntrySelect, forkPending,
  };
}

export type ComposerEditor = ReturnType<typeof useComposerEditor>;
