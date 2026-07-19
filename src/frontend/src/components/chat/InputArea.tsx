import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Dropdown, Switch } from 'antd';
import { AnimatePresence, motion } from 'motion/react';
import { DUR, EASE } from '../../utils/motionTokens';
import {
  FileImageOutlined, FileTextOutlined, CloudDownloadOutlined,
  AppstoreOutlined, FolderOutlined, FolderOpenOutlined, FolderAddOutlined, RobotOutlined,
  OrderedListOutlined, ThunderboltOutlined, ApiOutlined, SwapOutlined, SyncOutlined,
} from '@ant-design/icons';
import { useChatStore, useFileStore, useUIStore, useModelCapabilitiesStore, useCatalogStore, useAuthStore, usePluginStore, useEditionStore } from '../../stores';
import { useProjectStore } from '../../stores/projectStore';
import { useAgentStore } from '../../stores/agentStore';
import type { ChatMode } from '../../stores/chatStore';
import { FileAttachmentCard, MySpaceImportModal } from '../file';
import CreateProjectModal from '../projects/CreateProjectModal';
import { getApiUrl } from '../../api';
import type { InstalledPluginItem } from '../../types';
import { AgentMentionPopup, useAgentMention } from '../agent';
import { SkillSlashPopup, useSkillSlash, type SlashEntry } from './SkillSlashPopup';
import LoopPlanBar from '../loop/LoopPlanBar';
import { t } from '../../i18n';

interface InputAreaProps {
  inputRef: React.RefObject<HTMLTextAreaElement | null>;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  send: () => void;
  abort?: () => void;
  continueLoop?: (chatId?: string) => void;
  handleFileSelect: (e: React.ChangeEvent<HTMLInputElement>, ref: React.RefObject<HTMLInputElement | null>) => void;
  removeFile: (index: number) => void;
  placeholder?: string;
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
  onEnterMode?: (mode: 'plan' | 'batch') => void;
  /** Currently selected mode (projectComposer project page only; drives the "selected" marker and the indicator pill). */
  activeMode?: 'plan' | 'batch' | null;
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

// ── Component ───────────────────────────────────────────────────────────

export function InputArea({
  inputRef, fileInputRef, send, abort, continueLoop, handleFileSelect, removeFile,
  placeholder = t('请输入你的问题，按Enter发送，Shift+Enter换行'),
  rows: _rows = 3,
  disableMention = false,
  projectComposer = false,
  forceSendMode = false,
  onEnterMode: onEnterModeProp,
  activeMode = null,
}: InputAreaProps) {
  const {
    input, setInput, sending: storeSending, chatMode, setChatMode,
    quotedFollowUp, setQuotedFollowUp,
    activeSkill, setActiveSkill, activePlugin, setActivePlugin, activeMention, setActiveMention,
    planMode, setPlanMode, loopMode, setLoopMode, currentChat, enterChatMode,
    currentChatId, bindChatProject, unbindChatProject,
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
  // Whether the main model supports reasoning_effort (the high/max tiers are controlled by the admin model provider's extra_config switch)
  const supportsReasoningEffort = useModelCapabilitiesStore(
    (s) => s.capabilities.supports_reasoning_effort,
  );
  const userModelSwitchEnabled = useModelCapabilitiesStore(
    (s) => s.capabilities.user_model_switch_enabled,
  );
  const selectableModels = useModelCapabilitiesStore(
    (s) => s.capabilities.user_selectable_models,
  );
  const selectedModelProviderId = useModelCapabilitiesStore((s) => s.selectedModelProviderId);
  const setSelectedModelProviderId = useModelCapabilitiesStore((s) => s.setSelectedModelProviderId);
  const { promptHubOpen, setPromptHubOpen } = useUIStore();
  const isCE = useEditionStore((s) => s.edition === 'ce');
  const _currentChat = currentChat();
  const isPlanChat = !!_currentChat?.planChat;
  const isBatchChat = !!_currentChat?.batchChat;
  const isSiteChat = !!_currentChat?.siteChat;
  // Whether the "autonomous loop" entry is shown: normal chat (not plan/batch/project page)
  // + has the loop capability bit + has lab permission. When eligible it no longer occupies
  // the toolbar but is tucked into the "+" attachment menu, visible to lab users only.
  const showLoopEntry =
    !isPlanChat && !isBatchChat && !projectComposer && loopCapEnabled !== false && labEnabled !== false;
  const imageInputRef = useRef<HTMLInputElement | null>(null);
  const [mySpaceImportOpen, setMySpaceImportOpen] = useState(false);

  // / slash candidates: installed plugins (first) + enabled skills (after), filtered by the
  // keyword typed after the slash. The same list is shared by keyboard Enter selection and
  // popup rendering, keeping selectedIndex consistent.
  const slashEntries = useMemo<SlashEntry[]>(() => {
    const q = input.startsWith('/') ? input.slice(1).toLowerCase() : '';
    const pluginEntries: SlashEntry[] = installedPlugins
      .filter((p) => !q || p.name.toLowerCase().includes(q))
      .map((p) => ({ kind: 'plugin' as const, id: p.install_id, name: p.name, plugin: p }));
    const skillEntries: SlashEntry[] = (skills || [])
      .filter((s) => s.enabled && (!q || s.name.toLowerCase().includes(q)))
      .map((s) => ({ kind: 'skill' as const, id: s.id, name: s.name }));
    return [...pluginEntries, ...skillEntries];
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
  function applyMention(agentName: string) {
    const ed = editorRef.current;
    if (!ed) return;
    insertChipAtCursor(ed, '@', agentName, 'jx-editorChip--mention');
    setActiveMention({ name: agentName });
    setMentionVisible(false);
    syncText();
    ed.focus();
  }

  function onMentionSelect(agentName: string) {
    const ed = editorRef.current;
    if (!ed) return;
    removeQueryAtCursor(ed, '@');
    applyMention(agentName);
  }

  /** Pick a sub-agent from the "+" menu: move the caret to the end first, then insert the chip. */
  function onPickAgentFromMenu(agentName: string) {
    const ed = editorRef.current;
    if (!ed) return;
    ed.focus();
    moveCaretToEnd(ed);
    applyMention(agentName);
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

  /** Enter plan / batch-execution mode from the "+" menu. The project page customizes this
   *  via the onEnterMode prop (defer chat creation until send, no navigation); the default
   *  switches the current chat to that mode in place — no new chat, no navigation, the
   *  current conversation becomes plan/batch mode where it is (avoids bouncing the whole
   *  chat back to the home page). */
  function onEnterMode(mode: 'plan' | 'batch') {
    if (onEnterModeProp) {
      onEnterModeProp(mode);
      return;
    }
    enterChatMode(mode, { inPlace: true });
  }

  // ── Keyboard ──
  function onKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    // Slash popup: Enter/Tab → select skill
    if (slashVisible && (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey))) {
      e.preventDefault();
      const sel = slashEntries[sIdx] || slashEntries[0];
      if (sel) {
        if (sel.kind === 'plugin' && sel.plugin) onSlashSelectPlugin(sel.plugin);
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
      if (sel) onMentionSelect(sel.name);
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

  const isEmpty = !input.trim() && !activeMention && !activeSkill && !activePlugin && !isComposing;

  const hasAttachments = uploadedFiles.length > 0 || importedSpaceFiles.length > 0;

  return (
    <div className="jx-inputArea">
      <LoopPlanBar onContinue={continueLoop} />
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
      <div className={`jx-composerWrap${isPlanChat && planMode ? ' jx-composerWrap--plan' : ''}`}>
        {!disableMention && (
          <AgentMentionPopup input={input} visible={mentionVisible} selectedIndex={mIdx} onSelect={onMentionSelect} onHover={setMIdx} />
        )}
        <SkillSlashPopup
          entries={slashEntries}
          visible={slashVisible}
          selectedIndex={sIdx}
          onSelect={(entry) => {
            if (entry.kind === 'plugin' && entry.plugin) onSlashSelectPlugin(entry.plugin);
            else onSlashSelect(entry.id, entry.name);
          }}
          onHover={setSIdx}
        />

        <input ref={fileInputRef} type="file" multiple style={{ display: 'none' }}
          accept=".pdf,.docx,.doc,.wps,.txt,.xlsx,.xls,.csv"
          onChange={(e) => handleFileSelect(e, fileInputRef)} />
        <input ref={imageInputRef} type="file" multiple style={{ display: 'none' }}
          accept="image/png,image/jpeg,image/gif,image/webp,image/bmp,image/svg+xml"
          onChange={(e) => handleFileSelect(e, imageInputRef)} />

        {/* ContentEditable editor — chips and text live on the same layer */}
        <div
          ref={editorRef}
          contentEditable
          suppressContentEditableWarning
          className={`jx-composer jx-composerEditor${isEmpty ? ' jx-composerEditor--empty' : ''}`}
          data-placeholder={placeholder}
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

        <div className="jx-composerBar">
          {(() => {
            const MODE_META: Record<ChatMode, { title: string; desc: string; label: string }> = {
              fast:   { title: t('快速模式'),   desc: t('适用于大部分情况'),              label: t('快速模式') },
              medium: { title: t('思考·中'),    desc: t('默认思考强度，兼顾速度与质量'),  label: t('思考·中') },
              high:   { title: t('思考·高'),    desc: t('更深入推理，处理复杂分析'),      label: t('思考·高') },
              max:    { title: t('思考·超高'),  desc: t('研究级别的专家智能体'),          label: t('思考·超高') },
            };
            // Multi-tier models show 4 items; models without multi-tier support only show "fast / thinking" (thinking maps to medium)
            const modeKeys: ChatMode[] = supportsReasoningEffort
              ? ['fast', 'medium', 'high', 'max']
              : ['fast', 'medium'];
            // Without multi-tier support, display high/max as medium
            const effectiveMode: ChatMode = supportsReasoningEffort
              ? chatMode
              : (chatMode === 'fast' ? 'fast' : 'medium');
            const isThinking = effectiveMode !== 'fast';
            const currentMeta = MODE_META[effectiveMode];
            const btnLabel = supportsReasoningEffort ? currentMeta.label : (isThinking ? t('思考模式') : t('快速模式'));
            const items = modeKeys.map((key) => {
              const meta = MODE_META[key];
              const isCurrent = effectiveMode === key;
              const optionTitle = !supportsReasoningEffort && key === 'medium' ? t('思考模式') : meta.title;
              return {
                key,
                label: (
                  <div className="jx-modeOption">
                    <div className="jx-modeOptionHead">
                      <span className="jx-modeOptionTitle">{optionTitle}</span>
                      {isCurrent && <img src="/home/check.svg" alt="" className="jx-modeCheckIcon" />}
                    </div>
                    <div className="jx-modeOptionDesc">{meta.desc}</div>
                  </div>
                ),
                onClick: () => setChatMode(key),
              };
            });
            return (
              <Dropdown
                menu={{ items, selectedKeys: [effectiveMode] }}
                trigger={['click']}
                placement="topLeft"
                overlayClassName="jx-modeMenu"
              >
                <button
                  className={`jx-modeDropBtn${isThinking ? ' thinking' : ''}`}
                  aria-label={t('当前为{label}，点击切换', { label: btnLabel })}
                >
                  <img src={isThinking ? '/home/thinking.svg' : '/home/quick.svg'} alt="" className="jx-modeIcon" />
                  <span>{btnLabel}</span>
                  <img src="/home/arrow-down.svg" alt="" className="jx-modeArrow" />
                </button>
              </Dropdown>
            );
          })()}

          {!isCE && (
            <button className="jx-promptHubBtn" onClick={() => setPromptHubOpen(!promptHubOpen)} aria-label={t('提示词中心')}>
              <img src="/home/prompt.svg" alt="" className="jx-promptHubIcon" />
              <span>{t('提示词中心')}</span>
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
                        <FolderOutlined className="jx-projectOptionIcon" />
                        <span className="jx-projectOptionName" title={p.name}>{p.name}</span>
                        {boundProjectId === p.project_id && <img src="/home/check.svg" alt="" className="jx-modeCheckIcon" />}
                      </div>
                    ),
                    onClick: () => onPickProject(p.project_id, p.name),
                  })),
                ],
              },
              { type: 'divider' as const },
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
            ];
            return (
              <Dropdown
                trigger={['click']}
                placement="topLeft"
                overlayClassName="jx-projectMenu"
                onOpenChange={(open) => { if (open && projects.length === 0) void fetchProjects(); }}
                menu={{ items: projectMenuItems }}
              >
                <button
                  type="button"
                  className={`jx-projectDropBtn${boundProjectId ? ' bound' : ''}`}
                  aria-label={boundProjectId
                    ? t('本对话属于项目「{name}」，点击切换', { name: boundProjectName })
                    : t('选择项目，当前为默认（不归属项目）')}
                  title={t('选择项目')}
                >
                  {boundProjectId
                    ? <FolderOpenOutlined className="jx-projectDropIcon" />
                    : <FolderOutlined className="jx-projectDropIcon" />}
                  <span className="jx-projectDropName">{boundProjectId ? boundProjectName : t('默认')}</span>
                  <img src="/home/arrow-down.svg" alt="" className="jx-modeArrow" />
                </button>
              </Dropdown>
            );
          })()}

          {isPlanChat && (
            <motion.button
              className={`jx-planModeBtn${planMode ? ' active' : ''}`}
              whileTap={{ scale: 0.96 }}
              onClick={() => setPlanMode(!planMode)}
              aria-label={t('计划模式')}
              title={planMode ? t('关闭计划模式：切换为普通对话') : t('开启计划模式：AI 将自动分解任务为多步骤并逐步执行')}
            >
              <span>{t('计划模式')}</span>
            </motion.button>
          )}

          {isBatchChat && (
            <div
              className="jx-planModeBtn active"
              role="status"
              aria-label={t('批量执行模式')}
              title={t('批量执行模式：描述要批量处理的对象与任务，AI 会自动生成可确认的执行计划')}
            >
              <span>{t('批量执行')}</span>
            </div>
          )}


          {showLoopEntry && loopMode && (
            <motion.button
              className="jx-planModeBtn active"
              whileTap={{ scale: 0.96 }}
              onClick={() => setLoopMode(false)}
              aria-label={t('自主循环')}
              title={t('关闭自主循环：切换为普通对话')}
            >
              <span>{t('自主循环')}</span>
            </motion.button>
          )}

          {projectComposer && activeMode && (
            <motion.button
              type="button"
              className="jx-planModeBtn active"
              whileTap={{ scale: 0.96 }}
              onClick={() => onEnterMode(activeMode)}
              aria-label={activeMode === 'plan' ? t('计划模式') : t('批量执行')}
              title={t('发送后将以该模式在本项目内开始对话；点击取消')}
            >
              <span>{activeMode === 'plan' ? t('计划模式') : t('批量执行')}</span>
            </motion.button>
          )}

          <div style={{ flex: 1 }} />

          {userModelSwitchEnabled && selectableModels.length > 0 && (() => {
            const currentModel = selectableModels.find((m) => m.provider_id === selectedModelProviderId)
              || selectableModels.find((m) => m.is_default)
              || selectableModels[0];
            const items = selectableModels.map((model) => ({
              key: model.provider_id,
              label: (
                <div className="jx-modelOption">
                  <div className="jx-modelOptionHead">
                    <span className="jx-modelOptionTitle">{model.display_name}</span>
                    {model.provider_id === currentModel.provider_id && (
                      <img src="/home/check.svg" alt="" className="jx-modeCheckIcon" />
                    )}
                  </div>
                  <div className="jx-modelOptionDesc">{model.model_name || model.provider}</div>
                </div>
              ),
              onClick: () => setSelectedModelProviderId(model.provider_id),
            }));
            return (
              <Dropdown
                trigger={['click']}
                placement="topRight"
                overlayClassName="jx-modelMenu"
                menu={{ items, selectedKeys: [currentModel.provider_id] }}
              >
                <button
                  type="button"
                  className="jx-modelDropBtn"
                  aria-label={t('当前模型：{name}，点击切换', { name: currentModel.display_name })}
                  title={t('切换模型')}
                >
                  <SwapOutlined className="jx-modelDropIcon" />
                  <span className="jx-modelDropText">{currentModel.display_name}</span>
                </button>
              </Dropdown>
            );
          })()}

          {(() => {
            // Mode entries (plan / batch), shared by the main menu and the project-page
            // projectComposer, each gated by allowed_apps. projectComposer (project page)
            // marks per the selected activeMode; the main menu marks per the current chat type.
            const planActive = projectComposer ? activeMode === 'plan' : isPlanChat;
            const batchActive = projectComposer ? activeMode === 'batch' : isBatchChat;
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
            ];
            const items = [
              { key: 'image', icon: <FileImageOutlined />, label: t('上传图片'), onClick: () => imageInputRef.current?.click() },
              { key: 'file', icon: <FileTextOutlined />, label: t('上传文件'), onClick: () => fileInputRef.current?.click() },
              { type: 'divider' as const },
              ...modeItems,
              ...(showLoopEntry ? [{
                key: 'mode-loop',
                icon: <SyncOutlined />,
                label: (
                  <span className="jx-attachMenu-toggleRow">
                    <span>{t('自主循环')}</span>
                    <Switch size="small" checked={loopMode} />
                  </span>
                ),
                onClick: () => setLoopMode(!loopMode),
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
                    onClick: () => onPickAgentFromMenu(a.name),
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
              { type: 'divider' as const },
              {
                key: 'myspace',
                icon: <CloudDownloadOutlined />,
                label: t('从我的空间导入'),
                onClick: () => setMySpaceImportOpen(true),
              },
            ];
            return (
              <>
                <Dropdown
                  trigger={['click']}
                  placement="topRight"
                  overlayClassName="jx-attachMenu"
                  onOpenChange={(open) => {
                    if (!open) return;
                    if (!disableMention && agents.length === 0) void fetchAgents();
                  }}
                  menu={{ items }}
                >
                  <button
                    className="jx-attachBtn"
                    title={t('添加文件')}
                    aria-label={t('添加文件')}
                  >
                    <img src="/home/attachment.svg" alt="" className="jx-attachIcon" />
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
          {/* Send ↔ abort: single button + icon crossfade (button hover/active scaling is
              done in CSS, motion only animates the inner icon, so they never conflict) */}
          <button
            className="jx-sendBtn"
            onClick={() => { if (sending) { abort?.(); } else { send(); } }}
            disabled={!sending && uploadingFiles.size > 0}
            aria-label={sending ? t('中止') : t('发送')}
          >
            <AnimatePresence mode="wait" initial={false}>
              <motion.img
                key={sending ? 'stop' : 'send'}
                src={sending ? '/home/stop.svg' : '/home/send.svg'}
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
