import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Dropdown } from 'antd';
import { AnimatePresence, motion } from 'motion/react';
import { DUR, EASE } from '../../utils/motionTokens';
import {
  FileImageOutlined, FileTextOutlined, CloudDownloadOutlined,
  AppstoreOutlined, FolderOutlined, FolderOpenOutlined, FolderAddOutlined, RobotOutlined,
  OrderedListOutlined, ThunderboltOutlined, ApiOutlined, SyncOutlined, PartitionOutlined,
  LaptopOutlined, CloseOutlined,
} from '@ant-design/icons';
import { useChatStore, useFileStore, useUIStore, useCatalogStore, useAuthStore, usePluginStore, useEditionStore } from '../../stores';
import { useProjectStore } from '../../stores/projectStore';
import { projectCreationTargets, useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { useAgentStore } from '../../stores/agentStore';
import type { UserAgentItem } from '../../stores/agentStore';
import { FileAttachmentCard, MySpaceImportModal } from '../file';
import CreateProjectModal from '../projects/CreateProjectModal';
import { getApiUrl, createLocalProject } from '../../api';
import type { InstalledPluginItem } from '../../types';
import { AgentMentionPopup, useAgentMention } from '../agent';
import { SkillSlashPopup, useSkillSlash, type SlashEntry } from './SkillSlashPopup';
import LoopPlanBar from '../loop/LoopPlanBar';
import { resolveBatchModeActive, resolveWorkflowModeActive } from '../../utils/chatMode';
import { useFileDropZone } from '../../hooks/useFileDropZone';
import { DropOverlay } from '../common/DropOverlay';
import { ChipChevron } from '../common/ChipChevron';
import { IconPlus } from '../common/DshIcons';
import LocalApprovalPill from './LocalApprovalPill';
import DeploymentSwitcher from './DeploymentSwitcher';
import ChatModeSwitch from './ChatModeSwitch';
import ModelEffortChip from './ModelEffortChip';
import { ContextGauge } from './ContextGauge';
import { QueuedMessageCard } from './QueuedMessageCard';
import { t } from '../../i18n';

interface InputAreaProps {
  inputRef: React.RefObject<HTMLTextAreaElement | null>;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  send: () => void;
  abort?: () => void;
  activateQueuedMessage?: (chatId?: string) => Promise<void>;
  discardQueuedMessage?: (chatId?: string) => Promise<void>;
  continueLoop?: (chatId?: string) => void;
  handleFileSelect: (e: React.ChangeEvent<HTMLInputElement>, ref: React.RefObject<HTMLInputElement | null>) => void;
  removeFile: (index: number) => void;
  placeholder?: string;
  mobilePlaceholder?: string;
  rows?: number;
  disableMention?: boolean;
  /** New-chat composer on the project page: hides the "Project" selector dropdown
   *  (the chat is fixed to the current project) and the autonomous-loop entry; mode
   *  items in the "+" menu are marked "selected" per activeMode. All other abilities
   *  (attachment upload / skills / plugins / @sub-agents / import from My Space) are
   *  identical to the main composer. */
  projectComposer?: boolean;
  /** Always show the send button, ignoring the current chat's streaming state. Used by
   *  the project-page composer: it is a "new-chat starting point" and should not reflect
   *  the state of some chat that is currently streaming. */
  forceSendMode?: boolean;
  /** Custom "enter plan/batch mode" behavior. The project page passes this in: defer
   *  chat creation until send, no navigation; when omitted, falls back to the default
   *  enterChatMode (switches the current chat in place). */
  onEnterMode?: (mode: 'plan' | 'batch' | 'workflow') => void;
  /** Currently selected mode (projectComposer project page only; drives the "selected" marker and the indicator pill). */
  activeMode?: 'plan' | 'batch' | 'workflow' | null;
}

// ── Attachment card keys ────────────────────────────────────────────────
// Assign each File object a stable auto-incrementing id as the animation key. The old
// key included the array index (idx), so deleting a middle item shifted keys of the
// following cards, making them replay the entrance animation as "new cards".
let fileKeySeq = 0;
const fileKeyMap = new WeakMap<File, number>();
function getFileKey(file: File): string {
  let id = fileKeyMap.get(file);
  if (id === undefined) {
    id = ++fileKeySeq;
    fileKeyMap.set(file, id);
  }
  return `upload-${id}`;
}

const attachCardMotion = {
  layout: true,
  initial: { opacity: 0, scale: 0.85 },
  animate: { opacity: 1, scale: 1 },
  exit: { opacity: 0, scale: 0.85, transition: { duration: 0.12, ease: EASE.exit } },
  transition: { duration: 0.18, ease: EASE.brandOut },
} as const;

// ── ContentEditable helpers ─────────────────────────────────────────────

/** Extract plain text from editor, skipping chip spans. */
function getEditorText(el: HTMLElement): string {
  let t = '';
  const walk = (n: Node) => {
    if (n.nodeType === Node.TEXT_NODE) {
      // Convert non-breaking spaces back to regular
      t += (n.textContent || '').replace(/\u00A0/g, ' ');
    } else if (n instanceof HTMLBRElement) {
      t += '\n';
    } else if (n instanceof HTMLElement) {
      if (n.dataset.chip) return; // skip chips
      const isBlock = n.tagName === 'DIV' || n.tagName === 'P';
      if (isBlock && t && !t.endsWith('\n')) t += '\n';
      for (const c of n.childNodes) walk(c);
    }
  };
  for (const c of el.childNodes) walk(c);
  return t;
}

/** Remove text backwards from cursor to the trigger char (@ or /). */
function removeQueryAtCursor(_editor: HTMLElement, trigger: string) {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0) return;
  const range = sel.getRangeAt(0);
  const node = range.startContainer;
  if (node.nodeType !== Node.TEXT_NODE) return;
  const text = node.textContent || '';
  const cursor = range.startOffset;
  const idx = text.lastIndexOf(trigger, cursor - 1);
  if (idx === -1) return;
  node.textContent = text.slice(0, idx) + text.slice(cursor);
  try {
    range.setStart(node, idx);
    range.collapse(true);
    sel.removeAllRanges();
    sel.addRange(range);
  } catch { /* empty text node edge case */ }
}

/** Insert an inline chip span at the current cursor, followed by a space. */
function insertChipAtCursor(editor: HTMLElement, prefix: string, name: string, cls: string, chipType?: string) {
  clearEditorIfOnlyBrowserEmptyNodes(editor);

  const chip = document.createElement('span');
  chip.contentEditable = 'false';
  chip.className = `jx-editorChip ${cls}`;
  chip.dataset.chip = chipType || (prefix === '@' ? 'mention' : 'skill');
  chip.dataset.chipName = name;
  chip.innerHTML =
    `<span class="jx-editorChip-prefix">${prefix}</span>` +
    `<span class="jx-editorChip-name">${name}</span>`;

  const space = document.createTextNode('\u00A0');
  const sel = window.getSelection();
  if (sel && sel.rangeCount > 0 && editor.contains(sel.getRangeAt(0).commonAncestorContainer)) {
    const range = sel.getRangeAt(0);
    range.collapse(true);
    const fragment = document.createDocumentFragment();
    fragment.append(chip, space);
    range.insertNode(fragment);
  } else {
    editor.appendChild(chip);
    editor.appendChild(space);
  }
  setCaretAfter(space);
}

function setEditorPlainText(editor: HTMLElement, text: string) {
  editor.innerHTML = '';
  if (text) {
    editor.textContent = text;
  }
}

function moveCaretToEnd(editor: HTMLElement) {
  const selection = window.getSelection();
  if (!selection) return;
  const range = document.createRange();
  range.selectNodeContents(editor);
  range.collapse(false);
  selection.removeAllRanges();
  selection.addRange(range);
}

function setCaretAfter(node: Node) {
  const selection = window.getSelection();
  if (!selection) return;
  const range = document.createRange();
  range.setStartAfter(node);
  range.collapse(true);
  selection.removeAllRanges();
  selection.addRange(range);
}

function clearEditorIfOnlyBrowserEmptyNodes(editor: HTMLElement) {
  if (editor.querySelector('[data-chip]')) return;
  if (getEditorText(editor).trim()) return;
  if (editor.childNodes.length > 0) {
    editor.replaceChildren();
  }
}

// ── Mode chip ───────────────────────────────────────────────────────────

/** The "you are in plan / batch / loop mode" chip in the composer bar. The chip body is a pure
 *  status indicator; the ✕ badge pinned to its top-right corner is the one way to leave the mode
 *  (the "+" menu only turns modes on, so this ✕ must always be reachable). */
function ModeChip({
  icon, label, title, closeLabel, onClose,
}: {
  icon: React.ReactNode;
  label: string;
  title: string;
  closeLabel: string;
  onClose: () => void;
}) {
  return (
    <motion.span
      className="jx-composerChip jx-planModeBtn jx-modeChip active"
      role="status"
      title={title}
      initial={{ opacity: 0, scale: 0.9 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: DUR.fast, ease: EASE.brandOut }}
    >
      {icon}
      <span className="jx-composerChip-label">{label}</span>
      <motion.button
        type="button"
        className="jx-modeChip-close"
        whileTap={{ scale: 0.88 }}
        onClick={onClose}
        aria-label={closeLabel}
        title={closeLabel}
      >
        <CloseOutlined />
      </motion.button>
    </motion.span>
  );
}

// ── Component ───────────────────────────────────────────────────────────

export function InputArea({
  inputRef, fileInputRef, send, abort, activateQueuedMessage, discardQueuedMessage, continueLoop, handleFileSelect, removeFile,
  placeholder = t('请输入你的问题，按Enter发送，Shift+Enter换行'),
  mobilePlaceholder,
  rows: _rows = 3,
  disableMention = false,
  projectComposer = false,
  forceSendMode = false,
  onEnterMode: onEnterModeProp,
  activeMode = null,
}: InputAreaProps) {
  const {
    input, setInput, sending: storeSending,
    quotedFollowUp, setQuotedFollowUp,
    activeSkill, setActiveSkill, activePlugin, setActivePlugin, activeMention, setActiveMention,
    planMode, loopMode, setLoopMode, currentChat, enterChatMode, exitChatMode,
    currentChatId, bindChatProject, unbindChatProject,
    queuedMessages, updateQueuedMessage, activeRuns,
  } = useChatStore();
  // Autonomous-loop capability bit (enabled by default): without permission the "autonomous loop" toggle is hidden
  const loopCapEnabled = useAuthStore((s) => s.authUser?.can_run_autonomous_loop);
  // Lab permission (undefined defaults to enabled): the autonomous loop is an experimental ability, only shown in lab users' chats
  const labEnabled = useAuthStore((s) => s.authUser?.lab_enabled);
  // Which apps are open to the current user (same allowed_apps gate as the "App Center")
  const allowedApps = useAuthStore((s) => s.authUser?.allowed_apps ?? null);
  const isAppAllowed = (id: string) => !Array.isArray(allowedApps) || allowedApps.includes(id);
  // Skill list (for the skills submenu of the "+" menu)
  const skills = useCatalogStore((s) => s.catalog.skills);
  // Project list (for the toolbar "Project" selector dropdown)
  const projects = useProjectStore((s) => s.list);
  const fetchProjects = useProjectStore((s) => s.fetchProjects);
  const setProjectCreateModalOpen = useProjectStore((s) => s.setCreateModalOpen);
  // Sub-agent list (for the "@sub-agent" submenu of the "+" menu)
  const agents = useAgentStore((s) => s.agents);
  const fetchAgents = useAgentStore((s) => s.fetchAgents);
  // Installed plugins (for the "Plugins" submenu of the "+" menu + the / slash popup).
  // Uses the shared store: the capability center forces a refresh after install/uninstall,
  // so this syncs immediately (avoids fetching only on mount, which would hide newly installed plugins).
  const installedPlugins = usePluginStore((s) => s.installed);
  useEffect(() => { void usePluginStore.getState().fetchInstalled(); }, []);
  const sending = forceSendMode ? false : storeSending;
  const { uploadedFiles, uploadingFiles, importedSpaceFiles, removeImportedSpaceFile } = useFileStore();
  const { promptHubOpen, setPromptHubOpen } = useUIStore();
  const isCE = useEditionStore((s) => s.edition === 'ce');
  const _currentChat = currentChat();
  // Batch mode as the composer currently runs it — the persistent batchChat marker is only its
  // default, so a chat the user took out of batch mode no longer counts as one here.
  const batchModeOn = resolveBatchModeActive(_currentChat);
  const workflowModeOn = resolveWorkflowModeActive(_currentChat);
  const isSiteChat = !!_currentChat?.siteChat;
  // Whether the "autonomous loop" entry is shown: normal chat (not plan/batch/project page)
  // + has the loop capability bit + has lab permission. When eligible it no longer occupies
  // the toolbar but is tucked into the "+" attachment menu, visible to lab users only.
  const showLoopEntry =
    !planMode && !batchModeOn && !projectComposer && loopCapEnabled !== false && labEnabled !== false;
  const imageInputRef = useRef<HTMLInputElement | null>(null);
  const [mySpaceImportOpen, setMySpaceImportOpen] = useState(false);
  // 工具条各下拉的展开态：只用于让 chip 亮起 + 箭头翻转，让"按钮/浮层"读起来是一体的
  const [projectOpen, setProjectOpen] = useState(false);
  const [attachOpen, setAttachOpen] = useState(false);
  // Module C: no My Space in local mode → hide "从我的空间导入".
  const activeLocalMode = useDeploymentModeStore((s) => s.activeLocal);
  const provisionMode = useDeploymentModeStore((s) => s.provisionMode);
  const isDesktopShell = useDeploymentModeStore((s) => s.isDesktop);
  const {
    cloud: canCreateCloudProject,
    local: canCreateLocalProject,
  } = projectCreationTargets(isDesktopShell, provisionMode);
  // 混合架构：双模式=云端身份 + 本机执行面，本地项目能力在 dual 下同样可用。
  const localCapable = canCreateLocalProject;
  const refreshDeploymentMode = useDeploymentModeStore((s) => s.refresh);
  useEffect(() => {
    refreshDeploymentMode();
  }, [refreshDeploymentMode]);

  // 项目下拉里的「新建本地项目」：跳壳的文件夹选择器（/__desktop/pick-local-folder），
  // 壳选完把路径以 hugagent:local-folder 事件回抛到页面；这里建项目、刷新列表并
  // 把当前对话直接绑定到新项目上（项目页 composer 不注册，避免双实例重复建）。
  useEffect(() => {
    if (projectComposer || !isDesktopShell || !localCapable) return;
    const onFolder = (e: Event) => {
      const path = (e as CustomEvent<string>).detail;
      if (!path) return;
      const name = path.split(/[/\\]/).filter(Boolean).pop() || '本地项目';
      createLocalProject({ name, local_path: path })
        .then((proj) => {
          void useProjectStore.getState().fetchProjects();
          const { currentChatId: chatId, bindChatProject: bind } = useChatStore.getState();
          if (chatId) bind(chatId, proj.project_id, proj.name);
        })
        .catch((err) => {
          alert('新建本地项目失败：' + (err?.message || err));
        });
    };
    window.addEventListener('hugagent:local-folder', onFolder as EventListener);
    return () => window.removeEventListener('hugagent:local-folder', onFolder as EventListener);
  }, [projectComposer, isDesktopShell, localCapable]);

  // / slash candidates: installed plugins (first) + enabled skills (after), filtered by the
  // keyword typed after the slash. The same list is shared by keyboard Enter selection and
  // popup rendering, keeping selectedIndex consistent.
  const slashEntries = useMemo<SlashEntry[]>(() => {
    const q = input.startsWith('/') ? input.slice(1).toLowerCase() : '';
    // 模式命令排在最前：它切换的是这段对话怎么跑，比挑一个技能更"重"，也更常被找。
    // 工作流模式必须由用户显式触发——不触发就不注册 run_job、不注入批量提示词。
    const modeEntries: SlashEntry[] = (
      [{ id: 'workflow', name: 'workflow', hint: t('工作流模式：批量作业') }] as const
    )
      .filter((m) => !q || m.id.includes(q) || m.hint.toLowerCase().includes(q))
      .map((m) => ({ kind: 'mode' as const, id: m.id, name: m.name, hint: m.hint }));
    const pluginEntries: SlashEntry[] = installedPlugins
      .filter((p) => !q || p.name.toLowerCase().includes(q))
      .map((p) => ({ kind: 'plugin' as const, id: p.install_id, name: p.name, plugin: p }));
    const skillEntries: SlashEntry[] = (skills || [])
      .filter((s) => s.enabled && (!q || s.name.toLowerCase().includes(q)))
      .map((s) => ({ kind: 'skill' as const, id: s.id, name: s.name }));
    return [...modeEntries, ...pluginEntries, ...skillEntries];
  }, [input, installedPlugins, skills]);

  // Object URLs for uploaded image files — revoked when files change
  const uploadedImageUrls = useMemo(() => {
    return uploadedFiles.map((f) => (f.type.startsWith('image/') ? URL.createObjectURL(f) : undefined));
  }, [uploadedFiles]);
  useEffect(() => {
    return () => { uploadedImageUrls.forEach((u) => u && URL.revokeObjectURL(u)); };
  }, [uploadedImageUrls]);

  const editorRef = useRef<HTMLDivElement>(null);
  const composingRef = useRef(false);
  const [isComposing, setIsComposing] = useState(false);
  const prevTextRef = useRef('');

  const {
    mentionVisible, setMentionVisible,
    selectedIndex: mIdx, setSelectedIndex: setMIdx,
    handleInputChange: mentionInputChange, handleKeyDown: mentionKeyDown,
    getFiltered: getMentionFiltered,
  } = useAgentMention();
  const {
    slashVisible, setSlashVisible,
    selectedIndex: sIdx, setSelectedIndex: setSIdx,
    handleSlashInputChange: slashInputChange, handleSlashKeyDown: slashKeyDown,
  } = useSkillSlash();

  // ── Sync editor text → store ──
  const syncTextRef = useRef<() => void>(() => {});
  syncTextRef.current = () => {
    if (!editorRef.current) return;
    const text = getEditorText(editorRef.current);
    const prev = prevTextRef.current;
    if (text === prev) return; // no change
    prevTextRef.current = text;
    setInput(text);
    if (!disableMention) mentionInputChange(text, prev);
    slashInputChange(text, prev);
  };
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

    setEditorPlainText(editor, input);
    prevTextRef.current = input;

    if (hadMentionChip && activeMention) setActiveMention(null);
    if (hadSkillChip && activeSkill) setActiveSkill(null);
    if (hadPluginChip && activePlugin) setActivePlugin(null);

    if (document.activeElement === editor) {
      moveCaretToEnd(editor);
    }
  }, [activeMention, activeSkill, activePlugin, input, setActiveMention, setActiveSkill, setActivePlugin]);

  // ── Site-building chat: insert the activated "site" plugin into the composer by default as a plugin-reference chip ──
  // After the pure plugin gating, a site-building chat "references" the site plugin by
  // default (enterSiteMode already called setActivePlugin). Here it is rendered as an
  // inline chip identical to plugins picked manually via / or @ (replacing the old
  // "@site" mode badge).
  //
  // Key point (fixes plugin references leaking across chats): the editor DOM is a single
  // element shared by all chats, so plugin chips do not disappear automatically on chat
  // switch. We enforce a **strong invariant** as the safety net — "a plugin chip must
  // correspond to an activePlugin": whenever activePlugin is empty (setCurrentChatId
  // already recomputed it to null when switching to a non-site chat, or the user deleted
  // the chip), remove all stale plugin chips from the editor. It does not depend on any
  // "did we switch" check, so nothing slips through. Conversely, in a site chat, insert
  // the site plugin chip when there is no draft and no chip yet.
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
    if (isSiteChat && !editor.querySelector('[data-chip="plugin"]') && !input.trim()) {
      insertChipAtCursor(editor, '/', activePlugin.name, 'jx-editorChip--plugin', 'plugin');
      syncText();
    }
  }, [isSiteChat, activePlugin, _currentChat?.id, input]);

  // ── Expose the editor as inputRef for external .focus() calls ──
  useEffect(() => {
    if (editorRef.current) {
      (inputRef as React.MutableRefObject<any>).current = editorRef.current;
    }
  }, []);

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

  /** 选中 / 面板里的模式命令（如 /workflow）：把已输入的 "/xxx" 抹掉，直接开启该模式。
   *  与技能/插件不同——模式不插 chip，它改的是这段对话怎么跑，开启状态由输入框上方的模式条表示。 */
  function onSlashSelectMode(modeId: string) {
    const ed = editorRef.current;
    if (ed) removeQueryAtCursor(ed, '/');
    setInput('');
    if (ed) setEditorPlainText(ed, '');
    setSlashVisible(false);
    if (modeId === 'workflow') onEnterMode('workflow');
  }

  /** Pick a skill from the "+" menu: move the caret to the end first, then insert the chip (the editor may not have focus when the menu closes). */
  function onPickSkillFromMenu(skillId: string, skillName: string) {
    const ed = editorRef.current;
    if (!ed) return;
    ed.focus();
    moveCaretToEnd(ed);
    applySkill(skillId, skillName);
  }

  /** Insert a plugin chip and set it as the currently active plugin (shared by the / popup and the "+" menu). On send, its skillIds expand into skill_ids. */
  function applyPlugin(p: InstalledPluginItem) {
    const ed = editorRef.current;
    if (!ed) return;
    insertChipAtCursor(ed, '/', p.name, 'jx-editorChip--plugin', 'plugin');
    setActivePlugin({ name: p.name, skillIds: p.skills || [], mcpIds: p.mcp || [] });
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

  // ── Project binding (toolbar "Project" selector dropdown, to the right of the Prompt Hub) ──
  const boundProjectId = _currentChat?.projectId;
  const boundProjectName =
    _currentChat?.projectName ||
    projects.find((p) => p.project_id === boundProjectId)?.name ||
    '';

  function onPickProject(projectId: string, projectName: string) {
    bindChatProject(currentChatId, projectId, projectName);
  }

  /** Whether the composer currently runs in this mode (main composer: live composer state;
   *  project composer: the pending selection passed in via activeMode). */
  function isModeOn(mode: 'plan' | 'batch' | 'workflow') {
    if (projectComposer) return activeMode === mode;
    if (mode === 'plan') return planMode;
    if (mode === 'workflow') return workflowModeOn;
    return batchModeOn;
  }

  /** Enter plan / batch-execution mode from the "+" menu. The project page customizes this via
   *  the onEnterMode prop (defer chat creation until send, no navigation); the default switches
   *  the current chat to that mode in place — no new chat, no navigation (avoids bouncing the
   *  whole chat back to the home page). */
  function onEnterMode(mode: 'plan' | 'batch' | 'workflow') {
    if (onEnterModeProp) {
      onEnterModeProp(mode);
      return;
    }
    enterChatMode(mode, { inPlace: true });
  }

  /** Close a running mode from the ✕ on its composer chip — the single, always-visible way out.
   *  On the project page the pending selection is owned by the parent, so hand the toggle back
   *  to it (onEnterModeProp flips the already-selected mode off). */
  function onCloseMode(mode: 'plan' | 'batch' | 'workflow') {
    if (onEnterModeProp) {
      onEnterModeProp(mode);
      return;
    }
    exitChatMode(mode);
  }

  // ── Keyboard ──
  function onKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    // Let the IME own every key while it is composing. In particular, Enter and
    // Tab may confirm a candidate instead of sending or selecting a popup item.
    if (composingRef.current || e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return;

    // Slash popup: Enter/Tab → select skill
    if (slashVisible && (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey))) {
      e.preventDefault();
      const sel = slashEntries[sIdx] || slashEntries[0];
      if (sel) {
        if (sel.kind === 'mode') onSlashSelectMode(sel.id);
        else if (sel.kind === 'plugin' && sel.plugin) onSlashSelectPlugin(sel.plugin);
        else onSlashSelect(sel.id, sel.name);
      }
      return;
    }
    // Slash popup: ArrowUp/Down/Escape
    if (slashVisible && slashKeyDown(e)) return;

    // Mention popup: Enter/Tab → select mention
    if (mentionVisible && (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey))) {
      e.preventDefault();
      const list = getMentionFiltered(input);
      const sel = list[mIdx] || list[0];
      if (sel) onMentionSelect(sel);
      return;
    }
    // Mention popup: Escape
    if (mentionVisible && e.key === 'Escape') {
      e.preventDefault();
      setMentionVisible(false);
      return;
    }
    // Mention popup: ArrowUp/Down
    if (!disableMention && mentionVisible) {
      mentionKeyDown(e, input);
      if (e.defaultPrevented) return;
    }

    // Backspace: if editor only has chip(s) and maybe whitespace, remove last chip
    if (e.key === 'Backspace') {
      const ed = editorRef.current;
      if (ed) {
        const text = getEditorText(ed).trim();
        if (!text) {
          // No real text — check if a chip exists to remove
          const chips = ed.querySelectorAll('[data-chip]');
          if (chips.length > 0) {
            const last = chips[chips.length - 1] as HTMLElement;
            const type = last.dataset.chip;
            // Remove the chip and the space after it
            if (last.nextSibling?.nodeType === Node.TEXT_NODE) last.nextSibling.remove();
            last.remove();
            if (type === 'mention') setActiveMention(null);
            if (type === 'skill') setActiveSkill(null);
            if (type === 'plugin') setActivePlugin(null);
            e.preventDefault();
            syncText();
            return;
          }
        }
      }
    }

    // Enter → send, Shift+Enter → newline
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
      return;
    }
  }

  const showPlaceholder = !input.trim() && !activeMention && !activeSkill && !activePlugin && !isComposing;

  const hasAttachments = uploadedFiles.length > 0 || importedSpaceFiles.length > 0;
  // A project-detail composer starts a separate chat and deliberately ignores
  // the currently selected chat's run state; do not leak that chat's queue
  // into this independent composer either.
  const queuedMessage = forceSendMode ? undefined : queuedMessages[currentChatId];
  const canSteerQueued = !!activeRuns[currentChatId]?.runId
    && !hasAttachments
    && !activeSkill
    && !activePlugin
    && !activeMention;

  // A terminal run can race with the steer response. Never leave the card in
  // an impossible "waiting for a tool boundary" state once this chat is idle.
  useEffect(() => {
    if (!sending && queuedMessage?.status === 'steering') {
      updateQueuedMessage(currentChatId, (current) => ({
        ...current,
        status: 'queued',
      }));
    }
  }, [currentChatId, queuedMessage?.status, sending, updateQueuedMessage]);

  const showStopButton = sending && !input.trim();

  // 拖文件到输入区直接作为附件上传，复用点击"浏览"的同一条 handleFileSelect 管线
  // （它只读 e.target.files，合成一个最小 change 事件即可）。
  const { dragActive, dropZoneProps } = useFileDropZone(true, (files) => {
    handleFileSelect(
      { target: { files } } as unknown as React.ChangeEvent<HTMLInputElement>,
      fileInputRef,
    );
  });

  return (
    <div className="jx-inputArea" {...dropZoneProps}>
      <DropOverlay active={dragActive} hint={t('松开即可添加为附件')} className="jx-inputArea-dropOverlay" iconSize={20} />
      {/* 项目页 composer 不显示云端/本机切换：会话在哪执行由项目本身决定（云端项目在云端、
          本地项目在本机），不在项目内提供切换入口 */}
      {!projectComposer && <LoopPlanBar onContinue={continueLoop} />}
      <AnimatePresence initial={false}>
        {queuedMessage && (
          <motion.div
            key={queuedMessage.id}
            initial={{ opacity: 0, y: 8, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 4, scale: 0.98 }}
            transition={{ duration: DUR.fast, ease: EASE.brandOut }}
          >
            <QueuedMessageCard
              queued={queuedMessage}
              running={sending}
              canSteer={canSteerQueued}
              onSteer={() => { void activateQueuedMessage?.(currentChatId); }}
              onDelete={() => { void discardQueuedMessage?.(currentChatId); }}
              onEdit={(content) => updateQueuedMessage(currentChatId, (current) => ({
                ...current,
                content,
              }))}
            />
          </motion.div>
        )}
      </AnimatePresence>
      {hasAttachments && (
        <div className="jx-inputAttachments">
          <AnimatePresence initial={false}>
            {uploadedFiles.map((file, idx) => (
              <motion.div key={getFileKey(file)} {...attachCardMotion}>
                <FileAttachmentCard
                  name={file.name}
                  loading={uploadingFiles.has(file)}
                  onClose={() => removeFile(idx)}
                  previewUrl={uploadedImageUrls[idx]}
                />
              </motion.div>
            ))}
            {(() => {
              // The same file can be imported more than once; numbering by order of
              // file_id occurrence keeps keys unique, and no array index is mixed in
              // (deleting a middle item no longer shifts later cards and replays their animation).
              const seen = new Map<string, number>();
              return importedSpaceFiles.map((file, idx) => {
                const nth = (seen.get(file.file_id) ?? 0) + 1;
                seen.set(file.file_id, nth);
                const previewUrl = file.type === 'image'
                  ? `${getApiUrl()}${file.download_url || `/files/${file.file_id}`}`
                  : undefined;
                return (
                  <motion.div key={`space-${file.file_id}-${nth}`} {...attachCardMotion}>
                    <FileAttachmentCard
                      name={file.name}
                      onClose={() => removeImportedSpaceFile(idx)}
                      previewUrl={previewUrl}
                    />
                  </motion.div>
                );
              });
            })()}
          </AnimatePresence>
        </div>
      )}
      {quotedFollowUp && (
        <div className="jx-inputQuote">
          <div className="jx-inputQuoteBadge">{t('追问引用')}</div>
          <div className="jx-inputQuoteText" title={quotedFollowUp.text}>{quotedFollowUp.text}</div>
          <button type="button" className="jx-inputQuoteRemove" onClick={() => setQuotedFollowUp(null)} aria-label={t('移除引用')}>×</button>
        </div>
      )}
      {/* 对话框上方独立一行：标准/极速二选一（常驻）+ 运行位置胶囊（仅桌面双模式）。
          极速与否决定整段对话的工具面和提示词，值得摆在框外常驻可见，而不是藏进下拉。 */}
      <div className="jx-runTargetRow">
        <ChatModeSwitch />
        {!projectComposer && <DeploymentSwitcher />}
      </div>
      <div className={`jx-composerWrap${planMode ? ' jx-composerWrap--plan' : ''}`}>
        {!disableMention && (
          <AgentMentionPopup input={input} visible={mentionVisible} selectedIndex={mIdx} onSelect={onMentionSelect} onHover={setMIdx} />
        )}
        <SkillSlashPopup
          entries={slashEntries}
          visible={slashVisible}
          selectedIndex={sIdx}
          onSelect={(entry) => {
            if (entry.kind === 'mode') onSlashSelectMode(entry.id);
            else if (entry.kind === 'plugin' && entry.plugin) onSlashSelectPlugin(entry.plugin);
            else onSlashSelect(entry.id, entry.name);
          }}
          onHover={setSIdx}
        />

        <input ref={fileInputRef} type="file" multiple style={{ display: 'none' }}
          onChange={(e) => handleFileSelect(e, fileInputRef)} />
        <input ref={imageInputRef} type="file" multiple style={{ display: 'none' }}
          accept="image/png,image/jpeg,image/gif,image/webp,image/bmp,image/svg+xml"
          onChange={(e) => handleFileSelect(e, imageInputRef)} />

        {/* ContentEditable editor — chips and text live on the same layer */}
        <div
          ref={editorRef}
          contentEditable
          suppressContentEditableWarning
          className="jx-composer jx-composerEditor"
          onInput={() => { if (!composingRef.current) syncText(); }}
          onCompositionStart={() => { composingRef.current = true; setIsComposing(true); }}
          onCompositionEnd={() => { composingRef.current = false; setIsComposing(false); syncText(); }}
          onKeyDown={onKeyDown}
          onPaste={(e) => {
            e.preventDefault();
            const text = e.clipboardData.getData('text/plain');
            document.execCommand('insertText', false, text);
          }}
          onBlur={() => { setTimeout(() => { setMentionVisible(false); setSlashVisible(false); }, 200); }}
        />
        {showPlaceholder && (
          <div
            className="jx-composerPlaceholder"
            data-placeholder={placeholder}
            data-mobile-placeholder={mobilePlaceholder || placeholder}
            aria-hidden="true"
          />
        )}

        <div className="jx-composerBar">
          {/* ➕：附件与能力入口，坐在工具条最左（参考稿把 add 放在左下角，
              和右下角的发送形成一对，输入区两端各一个圆钮）*/}
          {(() => {
            // Mode entries (plan / batch), shared by the main menu and the project-page
            // projectComposer, each gated by allowed_apps. A chat can retain historical plan
            // cards after the user returns to ordinary conversation, so the main menu must use
            // the active composer mode rather than the persistent planChat classification.
            // The menu only turns a mode **on** and marks the one already running; turning it
            // off is the job of the ✕ on the mode chip down in the composer bar.
            const planActive = isModeOn('plan');
            const batchActive = isModeOn('batch');
            const workflowActive = isModeOn('workflow');
            const activeSuffix = projectComposer ? t('（已选）') : t('（已开启）');
            const modeItems = [
              ...(isAppAllowed('plan_mode') ? [{
                key: 'mode-plan',
                icon: <OrderedListOutlined />,
                label: planActive ? t('计划模式{suffix}', { suffix: activeSuffix }) : t('计划模式'),
                onClick: () => onEnterMode('plan'),
              }] : []),
              ...(isAppAllowed('batch_runner') ? [{
                key: 'mode-batch',
                icon: <ThunderboltOutlined />,
                label: batchActive ? t('批量执行{suffix}', { suffix: activeSuffix }) : t('批量执行'),
                onClick: () => onEnterMode('batch'),
              }] : []),
              // 工作流模式：面对成百上千个同构工作项时，让智能体写一段作业脚本交后台并发跑。
              // 与计划模式/批量执行一样是**用户显式触发**的模式——不进入就不注册 run_job、
              // 不注入批量提示词，普通问答完全不受影响。
              {
                key: 'mode-workflow',
                icon: <PartitionOutlined />,
                label: workflowActive ? t('工作流模式{suffix}', { suffix: activeSuffix }) : t('工作流模式'),
                onClick: () => onEnterMode('workflow'),
              },
            ];
            const items = [
              { key: 'image', icon: <FileImageOutlined />, label: t('上传图片'), onClick: () => imageInputRef.current?.click() },
              { key: 'file', icon: <FileTextOutlined />, label: t('上传文件'), onClick: () => fileInputRef.current?.click() },
              { type: 'divider' as const },
              ...modeItems,
              ...(showLoopEntry ? [{
                key: 'mode-loop',
                icon: <SyncOutlined />,
                label: loopMode ? t('自主循环{suffix}', { suffix: activeSuffix }) : t('自主循环'),
                onClick: () => setLoopMode(true),
              }] : []),
              ...((modeItems.length > 0 || showLoopEntry) ? [{ type: 'divider' as const }] : []),
              ...(!disableMention ? [{
                key: 'agents',
                icon: <RobotOutlined />,
                label: t('@子智能体'),
                children: (() => {
                  const enabled = (agents || []).filter((a) => a.is_enabled);
                  if (enabled.length === 0) {
                    return [{ key: 'agents-empty', label: t('暂无可用子智能体'), disabled: true }];
                  }
                  return enabled.map((a) => ({
                    key: `agent-${a.agent_id}`,
                    label: a.name,
                    onClick: () => onPickAgentFromMenu(a),
                  }));
                })(),
              }] : []),
              {
                key: 'skills',
                icon: <AppstoreOutlined />,
                label: t('技能'),
                children: (() => {
                  const enabled = (skills || []).filter((s) => s.enabled);
                  if (enabled.length === 0) {
                    return [{ key: 'skills-empty', label: t('暂无可用技能'), disabled: true }];
                  }
                  return enabled.map((s) => ({
                    key: `skill-${s.id}`,
                    label: s.name,
                    onClick: () => onPickSkillFromMenu(s.id, s.name),
                  }));
                })(),
              },
              {
                key: 'plugins',
                icon: <ApiOutlined />,
                label: t('插件'),
                children: (() => {
                  if (installedPlugins.length === 0) {
                    return [{ key: 'plugins-empty', label: t('暂无已安装插件'), disabled: true }];
                  }
                  return installedPlugins.map((p) => ({
                    key: `plugin-${p.install_id}`,
                    label: p.name,
                    onClick: () => onPickPluginFromMenu(p),
                  }));
                })(),
              },
              ...(activeLocalMode
                ? []
                : [
                    { type: 'divider' as const },
                    {
                      key: 'myspace',
                      icon: <CloudDownloadOutlined />,
                      label: t('从我的空间导入'),
                      onClick: () => setMySpaceImportOpen(true),
                    },
                  ]),
            ];
            return (
              <>
                <Dropdown
                  trigger={['click']}
                  placement="topRight"
                  overlayClassName="jx-attachMenu"
                  onOpenChange={(open) => {
                    setAttachOpen(open);
                    if (!open) return;
                    if (!disableMention && agents.length === 0) void fetchAgents();
                  }}
                  menu={{ items }}
                >
                  <button
                    type="button"
                    className={`jx-attachBtn${attachOpen ? ' open' : ''}`}
                    title={t('添加文件')}
                    aria-label={t('添加文件')}
                  >
                    <IconPlus size={16} className="jx-attachIcon" />
                  </button>
                </Dropdown>
                <MySpaceImportModal open={mySpaceImportOpen} onClose={() => setMySpaceImportOpen(false)} />
                {/* Toolbar "create personal project" in-place modal: after a successful
                    creation, automatically binds the current chat to the new project
                    (not rendered on the project page — the project selector dropdown is
                    hidden there and the chat is fixed to the current project) */}
                {!projectComposer && (
                  <CreateProjectModal
                    onCreated={(pid) => {
                      const created = useProjectStore.getState().list.find((p) => p.project_id === pid);
                      bindChatProject(currentChatId, pid, created?.name || t('项目'));
                    }}
                  />
                )}
              </>
            );
          })()}


          {/* 提示词中心：社区版没有这个能力；商业版按 Config「权限配置 → 应用可见范围」
              的 prompt_hub 位放行（allowed_apps 为空 = 不限制，等同全员可见）。 */}
          {!isCE && isAppAllowed('prompt_hub') && (
            <button
              type="button"
              className={`jx-composerChip jx-promptHubBtn${promptHubOpen ? ' active' : ''}`}
              onClick={() => setPromptHubOpen(!promptHubOpen)}
              aria-label={t('提示词中心')}
              aria-pressed={promptHubOpen}
            >
              <img src="/home/prompt.svg" alt="" className="jx-promptHubIcon" />
              <span className="jx-composerChip-label">{t('提示词中心')}</span>
            </button>
          )}

          {!projectComposer && (() => {
            // Project selector dropdown: default (no project bound) / bound to a project / create a new personal project.
            // Binding state uses chat.projectId as the single source of truth; project_id is attached automatically when sending messages.
            const projectMenuItems = [
              {
                key: 'proj-group',
                type: 'group' as const,
                label: t('项目'),
                children: [
                  {
                    key: 'proj-default',
                    label: (
                      <div className="jx-projectOption">
                        <FolderOutlined className="jx-projectOptionIcon" />
                        <span className="jx-projectOptionName">{t('默认')}</span>
                        {!boundProjectId && <img src="/home/check.svg" alt="" className="jx-modeCheckIcon" />}
                      </div>
                    ),
                    onClick: () => unbindChatProject(currentChatId),
                  },
                  ...projects.map((p) => ({
                    key: `proj-${p.project_id}`,
                    label: (
                      <div className="jx-projectOption">
                        {(p.kind as string) === 'local'
                          ? <LaptopOutlined className="jx-projectOptionIcon" />
                          : <FolderOutlined className="jx-projectOptionIcon" />}
                        <span className="jx-projectOptionName" title={p.name}>{p.name}</span>
                        {boundProjectId === p.project_id && <img src="/home/check.svg" alt="" className="jx-modeCheckIcon" />}
                      </div>
                    ),
                    onClick: () => onPickProject(p.project_id, p.name),
                  })),
                ],
              },
              { type: 'divider' as const },
              // 新建入口严格跟随安装时选择的运行形态：纯本机只显示本地项目，纯云端
              // 只显示云端项目，只有双模式同时显示两者。网页端保持原来的个人项目入口。
              ...(isDesktopShell
                ? [
                    ...(canCreateCloudProject
                      ? [{
                          key: 'proj-new-cloud',
                          label: (
                            <div className="jx-projectOption">
                              <FolderAddOutlined className="jx-projectOptionIcon" />
                              <span className="jx-projectOptionName">{t('新建云端项目')}</span>
                            </div>
                          ),
                          onClick: () => setProjectCreateModalOpen(true),
                        }]
                      : []),
                    ...(canCreateLocalProject
                      ? [{
                          key: 'proj-new-local',
                          label: (
                            <div className="jx-projectOption">
                              <LaptopOutlined className="jx-projectOptionIcon" />
                              <span className="jx-projectOptionName">{t('新建本地项目')}</span>
                            </div>
                          ),
                          onClick: () => {
                            window.location.href = '/__desktop/pick-local-folder';
                          },
                        }]
                      : []),
                  ]
                : [
                    {
                      key: 'proj-new',
                      label: (
                        <div className="jx-projectOption">
                          <FolderAddOutlined className="jx-projectOptionIcon" />
                          <span className="jx-projectOptionName">{t('新建个人项目')}</span>
                        </div>
                      ),
                      onClick: () => setProjectCreateModalOpen(true),
                    },
                  ]),
            ];
            return (
              <Dropdown
                trigger={['click']}
                placement="topLeft"
                overlayClassName="jx-projectMenu"
                onOpenChange={(open) => {
                  setProjectOpen(open);
                  if (open && projects.length === 0) void fetchProjects();
                }}
                menu={{ items: projectMenuItems }}
              >
                <button
                  type="button"
                  className={`jx-composerChip jx-projectDropBtn${boundProjectId ? ' bound' : ''}${projectOpen ? ' open' : ''}`}
                  aria-label={boundProjectId
                    ? t('本对话属于项目「{name}」，点击切换', { name: boundProjectName })
                    : t('选择项目，当前为默认（不归属项目）')}
                  title={t('选择项目')}
                >
                  {boundProjectId
                    ? <FolderOpenOutlined className="jx-projectDropIcon" />
                    : <FolderOutlined className="jx-projectDropIcon" />}
                  <span className="jx-projectDropName jx-composerChip-label">{boundProjectId ? boundProjectName : t('默认')}</span>
                  <ChipChevron />
                </button>
              </Dropdown>
            );
          })()}

      {!projectComposer && <LocalApprovalPill />}

          {/* Mode chips report "you are in this mode" and carry their own ✕ at the top-right —
              that ✕ is the way out, so an accidentally started mode is always one click from
              being cancelled. The chip only renders while its mode is actually running; a plan
              chat keeping its historical plan cards shows nothing once back to normal chat. */}
          {!projectComposer && planMode && (
            <ModeChip
              icon={<OrderedListOutlined className="jx-planModeIcon" />}
              label={t('计划模式')}
              title={t('计划模式：AI 将自动分解任务为多步骤并逐步执行')}
              closeLabel={t('关闭计划模式：切换为普通对话')}
              onClose={() => onCloseMode('plan')}
            />
          )}

          {!projectComposer && batchModeOn && (
            <ModeChip
              icon={<ThunderboltOutlined className="jx-planModeIcon" />}
              label={t('批量执行')}
              title={t('批量执行模式：描述要批量处理的对象与任务，AI 会自动生成可确认的执行计划')}
              closeLabel={t('关闭批量执行：切换为普通对话')}
              onClose={() => onCloseMode('batch')}
            />
          )}

          {!projectComposer && workflowModeOn && (
            <ModeChip
              icon={<PartitionOutlined className="jx-planModeIcon" />}
              label={t('工作流模式')}
              title={t('工作流模式：面对成百上千个同类工作项时，AI 会写一段作业脚本交给后台并发处理，进度记在台账上，中断可续跑')}
              closeLabel={t('关闭工作流模式：切换为普通对话')}
              onClose={() => onCloseMode('workflow')}
            />
          )}

          {showLoopEntry && loopMode && (
            <ModeChip
              icon={<SyncOutlined className="jx-planModeIcon" />}
              label={t('自主循环')}
              title={t('自主循环：描述一个可验证目标，AI 会反复迭代、自我修正，达标或触预算即停')}
              closeLabel={t('关闭自主循环：切换为普通对话')}
              onClose={() => setLoopMode(false)}
            />
          )}

          {projectComposer && activeMode && (
            <ModeChip
              icon={activeMode === 'plan'
                ? <OrderedListOutlined className="jx-planModeIcon" />
                : <ThunderboltOutlined className="jx-planModeIcon" />}
              label={activeMode === 'plan' ? t('计划模式') : t('批量执行')}
              title={t('发送后将以该模式在本项目内开始对话')}
              closeLabel={t('取消该模式')}
              onClose={() => onCloseMode(activeMode)}
            />
          )}

          <div className="jx-composerSpacer" style={{ flex: 1 }} />

          {/* 模型 + 思考强度：两级菜单，收起态一个 chip 同时报出两者 */}
          <ModelEffortChip />

          {/* Context-usage ring: estimated context-window occupancy for the current conversation */}
          <ContextGauge />

          {/* While a run is active, an empty composer keeps the stop button; typing switches
              back to send so Enter/click can queue a follow-up without cancelling the run. */}
          <button
            className="jx-sendBtn"
            onClick={() => { if (showStopButton) { abort?.(); } else { send(); } }}
            disabled={!showStopButton && uploadingFiles.size > 0}
            aria-label={showStopButton ? t('中止') : t('发送')}
          >
            <AnimatePresence mode="wait" initial={false}>
              <motion.img
                key={showStopButton ? 'stop' : 'send'}
                src={showStopButton ? '/home/stop.svg' : '/home/send.svg'}
                alt=""
                className="jx-sendIcon"
                initial={{ scale: 0.6, opacity: 0, rotate: -90 }}
                animate={{ scale: 1, opacity: 1, rotate: 0 }}
                exit={{ scale: 0.6, opacity: 0, rotate: 90 }}
                transition={{ duration: DUR.fast, ease: 'easeOut' }}
              />
            </AnimatePresence>
          </button>
        </div>
      </div>
    </div>
  );
}
