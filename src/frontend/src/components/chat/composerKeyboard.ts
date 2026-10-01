import type React from 'react';
import { handleComposerNewline } from './composerEditingKeys';
import { richEditor } from './composerRichText';
import { getEditorText, removeChipsOfType } from './composerEditorDom';
import type { ComposerState } from './useComposerState';
import type { ComposerSuggestions } from './useComposerSuggestions';
import type { ComposerEditor } from './useComposerEditor';

export function createComposerKeyHandler(
  state: ComposerState, suggestions: ComposerSuggestions, editor: ComposerEditor,
) {
  const {
    setActiveMention, setActiveSkill, setActivePlugin, setActiveConnector,
    setActiveCommand, removeReferencedChat,
  } = state;
  const {
    slashVisible, slashEntries, sIdx, slashKeyDown, mentionVisible, mentionCandidates,
    mIdx, mentionScreen, backToMentionRoot, setMentionVisible, mentionKeyDown,
  } = suggestions;
  const {
    composingRef, onSlashEntrySelect, onMentionCandidateSelect, editorRef, syncText, sendFromComposer,
  } = editor;
  // ── Keyboard ──
  return function onKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    // Let the IME own every key while it is composing. In particular, Enter and
    // Tab may confirm a candidate instead of sending or selecting a popup item.
    if (composingRef.current || e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return;

    const rich = editorRef.current ? richEditor(editorRef.current) : undefined;
    if (e.key === 'Enter' && e.shiftKey && rich) {
      e.preventDefault();
      handleComposerNewline(rich);
      return;
    }

    // Slash popup: Enter/Tab → select skill
    if (slashVisible && slashEntries.length > 0 && (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey))) {
      e.preventDefault();
      const sel = slashEntries[sIdx] || slashEntries[0];
      if (sel) onSlashEntrySelect(sel);
      return;
    }
    // Slash popup: ArrowUp/Down/Escape
    if (slashVisible && slashKeyDown(e, slashEntries.length)) return;

    // @ launcher: Enter/Tab → run the selected action or mention the selected agent
    if (mentionVisible && mentionCandidates.length > 0
      && (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey))) {
      e.preventDefault();
      const selected = mentionCandidates[mIdx] || mentionCandidates[0];
      if (selected) onMentionCandidateSelect(selected);
      return;
    }
    // In the agent second level, Escape goes back once; at the root it closes the launcher.
    if (mentionVisible && e.key === 'Escape') {
      e.preventDefault();
      if (mentionScreen === 'agents') backToMentionRoot();
      else setMentionVisible(false);
      return;
    }
    // @ launcher: ArrowUp/Down
    if (mentionVisible) {
      mentionKeyDown(e);
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
            if (type) removeChipsOfType(ed, type);
            if (type === 'mention') setActiveMention(null);
            if (type === 'skill') setActiveSkill(null);
            if (type === 'plugin') setActivePlugin(null);
            if (type === 'connector') setActiveConnector(null);
            if (type === 'command') setActiveCommand(null);
            if (type === 'chat' && last.dataset.chipId) removeReferencedChat(last.dataset.chipId);
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
      sendFromComposer();
      return;
    }
  };

}
