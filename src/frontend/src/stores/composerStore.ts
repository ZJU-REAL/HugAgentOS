import { useMemo } from 'react';
import { create } from 'zustand';
import type { JSONContent } from '@tiptap/core';
import type { ReferencableChat } from '../types';
import type { ChatCommand } from '../utils/projectCommands';
import type { UploadedAttachment } from '../utils/fileParser';

export interface ImportedSpaceFile {
  origin?: 'local' | 'cloud';
  name: string;
  file_id: string;
  download_url: string;
  mime_type: string;
  type: 'document' | 'image';
}
type Capability = { id: string; name: string } | null;
export interface ComposerDraft {
  input: string;
  document: JSONContent | null;
  quotedFollowUp: { text: string; ts: number } | null;
  activeSkill: Capability;
  activePlugin: Capability;
  activeConnector: Capability;
  activeMention: Capability;
  activeCommand: ChatCommand | null;
  referencedChats: ReferencableChat[];
  uploadedFiles: File[];
  uploadedArtifacts: Map<File, UploadedAttachment>;
  uploadingFiles: Set<File>;
  importedSpaceFiles: ImportedSpaceFile[];
  uploads: Map<File, Promise<UploadedAttachment>>;
}
function emptyDraft(): ComposerDraft {
  return {
    input: '', document: null, quotedFollowUp: null,
    activeSkill: null, activePlugin: null, activeConnector: null, activeMention: null,
    activeCommand: null, referencedChats: [], uploadedFiles: [], uploadedArtifacts: new Map(),
    uploadingFiles: new Set(), importedSpaceFiles: [], uploads: new Map(),
  };
}
let nextDraftId = 0;
const EMPTY_DRAFT = emptyDraft();
export const chatDraftKey = (id: string) => 'chat:' + id;
export const projectDraftKey = (id: string) => 'project:' + id;
interface ComposerState {
  userId: string | null;
  epoch: number;
  activeKey: string;
  scopes: Record<string, string>;
  drafts: Record<string, ComposerDraft>;
  activate: (key: string, initial?: Partial<ComposerDraft>) => void;
  resetForUser: (userId: string | null) => void;
  remove: (key: string) => void;
  transfer: (from: string, to: string) => void;
}
export const useComposerStore = create<ComposerState>((set, get) => ({
  userId: null, epoch: 0, activeKey: '', scopes: {}, drafts: {},
  activate: (key, initial) => set(s => {
    if (s.scopes[key]) return { activeKey: key };
    const ownerId = String(++nextDraftId);
    return { activeKey: key, scopes: { ...s.scopes, [key]: ownerId },
      drafts: { ...s.drafts, [ownerId]: { ...emptyDraft(), ...initial } } };
  }),
  resetForUser: userId => {
    if (get().userId === userId) return;
    set(s => ({ userId, epoch: s.epoch + 1, activeKey: '', scopes: {}, drafts: {} }));
  },
  remove: key => set(s => {
    const drafts = { ...s.drafts }, scopes = { ...s.scopes };
    delete drafts[scopes[key]];
    delete scopes[key];
    return { drafts, scopes };
  }),
  transfer: (from, to) => set(s => {
    const ownerId = s.scopes[from];
    if (!ownerId) return s;
    const scopes = { ...s.scopes, [to]: ownerId }, drafts = { ...s.drafts };
    if (s.scopes[to] && s.scopes[to] !== ownerId) delete drafts[s.scopes[to]];
    delete scopes[from];
    return { scopes, drafts, activeKey: to };
  }),
}));

/** Bound to an owner and account epoch: async completions cannot touch another draft or user. */
export function composerActions(ownerId: string, epoch = useComposerStore.getState().epoch) {
  const patch = (change: Partial<ComposerDraft> | ((draft: ComposerDraft) => Partial<ComposerDraft>)) => {
    useComposerStore.setState(s => {
      const draft = s.drafts[ownerId];
      if (s.epoch !== epoch || !draft) return s;
      const next = typeof change === 'function' ? change(draft) : change;
      return { drafts: { ...s.drafts, [ownerId]: { ...draft, ...next } } };
    });
  };
  return {
    patch,
    setInput: (input: string) => patch({
      input, document: null, activeSkill: null, activePlugin: null, activeConnector: null,
      activeMention: null, activeCommand: null, referencedChats: [],
    }),
    setQuotedFollowUp: (quotedFollowUp: ComposerDraft['quotedFollowUp']) => patch({ quotedFollowUp }),
    setActiveSkill: (activeSkill: Capability) => patch({ activeSkill }),
    setActivePlugin: (activePlugin: Capability) => patch({ activePlugin }),
    setActiveConnector: (activeConnector: Capability) => patch({ activeConnector }),
    setActiveMention: (activeMention: Capability) => patch({ activeMention }),
    setActiveCommand: (activeCommand: ChatCommand | null) => patch({ activeCommand }),
    addReferencedChat: (chat: ReferencableChat) => patch(d => ({
      referencedChats: d.referencedChats.some(c => c.chat_id === chat.chat_id) ? d.referencedChats : [...d.referencedChats, chat],
    })),
    removeReferencedChat: (id: string) => patch(d => ({ referencedChats: d.referencedChats.filter(c => c.chat_id !== id) })),
    setUploadedFiles: (uploadedFiles: File[]) => patch(d => ({
      uploadedFiles, uploadedArtifacts: new Map([...d.uploadedArtifacts].filter(([f]) => uploadedFiles.includes(f))),
    })),
    setUploadedArtifact: (file: File, artifact: UploadedAttachment) => patch(d => ({
      uploadedArtifacts: d.uploadedFiles.includes(file) ? new Map(d.uploadedArtifacts).set(file, artifact) : d.uploadedArtifacts,
    })),
    addUploadingFile: (file: File) => patch(d => ({ uploadingFiles: new Set(d.uploadingFiles).add(file) })),
    removeUploadingFile: (file: File) => patch(d => {
      const uploadingFiles = new Set(d.uploadingFiles); uploadingFiles.delete(file);
      return { uploadingFiles };
    }),
    addImportedSpaceFiles: (files: ImportedSpaceFile[]) => patch(d => ({ importedSpaceFiles: [...d.importedSpaceFiles, ...files] })),
    removeImportedSpaceFile: (index: number) => patch(d => ({ importedSpaceFiles: d.importedSpaceFiles.filter((_, i) => i !== index) })),
    /** Consume the captured turn; retain edits and attachments added while awaiting uploads. */
    consume: (snapshot: ComposerDraft, parts: readonly ('text' | 'invocation' | 'attachments' | 'references')[] = ['text', 'invocation', 'attachments', 'references']) => patch(d => {
      const textUnchanged = parts.includes('text') && d.input === snapshot.input && d.document === snapshot.document;
      const fields = ['activeSkill', 'activePlugin', 'activeConnector', 'activeMention', 'activeCommand'] as const;
      const cleared = parts.includes('invocation') ? Object.fromEntries(fields.filter(f => d[f] === snapshot[f]).map(f => [f, null])) : {};
      const uploadedFiles = parts.includes('attachments') ? d.uploadedFiles.filter(f => !snapshot.uploadedFiles.includes(f)) : d.uploadedFiles;
      if (parts.includes('attachments')) for (const f of snapshot.uploadedFiles) d.uploads.delete(f);
      return {
        ...cleared,
        quotedFollowUp: parts.includes('references') && d.quotedFollowUp === snapshot.quotedFollowUp ? null : d.quotedFollowUp,
        ...(textUnchanged ? { input: '', document: null } : {}),
        referencedChats: parts.includes('references') ? d.referencedChats.filter(c => !snapshot.referencedChats.includes(c)) : d.referencedChats,
        uploadedFiles,
        uploadedArtifacts: new Map([...d.uploadedArtifacts].filter(([f]) => uploadedFiles.includes(f))),
        uploadingFiles: parts.includes('attachments') ? new Set([...d.uploadingFiles].filter(f => !snapshot.uploadedFiles.includes(f))) : d.uploadingFiles,
        importedSpaceFiles: parts.includes('attachments') ? d.importedSpaceFiles.filter(f => !snapshot.importedSpaceFiles.includes(f)) : d.importedSpaceFiles,
      };
    }),
  };
}
export function readComposer(key = useComposerStore.getState().activeKey) {
  const store = useComposerStore.getState();
  return { ...store.drafts[store.scopes[key]] ?? EMPTY_DRAFT, ...composerActions(store.scopes[key], store.epoch), key };
}
export function useComposerDraft(key?: string) {
  const activeKey = useComposerStore(s => key ?? s.activeKey);
  const ownerId = useComposerStore(s => s.scopes[activeKey]);
  const draft = useComposerStore(s => s.drafts[ownerId] ?? EMPTY_DRAFT);
  const epoch = useComposerStore(s => s.epoch);
  const actions = useMemo(() => composerActions(ownerId, epoch), [ownerId, epoch]);
  return { ...draft, ...actions, key: activeKey };
}
