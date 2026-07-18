import { create } from 'zustand';
import type { KBDocument, KBChunk, ChunkPreviewResult } from '../types';

interface KBState {
  // ── Document browsing ──
  kbDocumentsMap: Record<string, KBDocument[]>;
  kbDocQuery: string;
  activeKbDoc: KBDocument | null;
  kbDocsLoadingId: string | null;
  kbDocDetailLoadingId: string | null;
  docDetailTab: 'content' | 'chunks';
  docChunks: KBChunk[];
  docChunksLoading: boolean;
  chunkSaving: string | null;

  // ── Create KB Modal ──
  createKBModalOpen: boolean;
  createKBName: string;
  createKBDesc: string;
  createKBLoading: boolean;

  // ── Upload Document Modal ──
  uploadDocModalOpen: boolean;
  uploadDocLoading: boolean;
  uploadDocFileList: File[];
  uploadParentChunkSize: number;
  uploadChildChunkSize: number;
  uploadOverlapTokens: number;
  uploadParentChildIndexing: boolean;
  uploadAutoKeywordsCount: number;
  uploadAutoQuestionsCount: number;
  // Parent chunk separators: one per line; leave empty to use the default hierarchy (only applies as a fallback for recursive chunking / semantic recursive chunking)
  uploadSeparators: string;
  // Child chunk separators: one per line, used to split child chunks under parent-child chunking; leave empty to use fixed-length sliding window
  uploadChildSeparators: string;
  uploadStep: 'config' | 'preview';
  uploadChunkMethod: string;
  chunkPreviewData: ChunkPreviewResult | null;
  chunkPreviewLoading: boolean;
  expandedChunkIndex: number | null;

  // ── Reindex Modal ──
  reindexModalOpen: boolean;
  reindexChunkMethod: string;
  reindexDocId: string | null;
  reindexKbId: string | null;
  reindexLoading: boolean;

  // ── Actions ──
  setKbDocumentsMap: (map: Record<string, KBDocument[]> | ((prev: Record<string, KBDocument[]>) => Record<string, KBDocument[]>)) => void;
  updateKbDocuments: (kbId: string, docs: KBDocument[]) => void;
  setKbDocQuery: (query: string) => void;
  setActiveKbDoc: (doc: KBDocument | null | ((prev: KBDocument | null) => KBDocument | null)) => void;
  setKbDocsLoadingId: (id: string | null) => void;
  setKbDocDetailLoadingId: (id: string | null) => void;
  setDocDetailTab: (tab: 'content' | 'chunks') => void;
  setDocChunks: (chunks: KBChunk[] | ((prev: KBChunk[]) => KBChunk[])) => void;
  setDocChunksLoading: (v: boolean) => void;
  setChunkSaving: (id: string | null) => void;

  // Create KB
  openCreateKBModal: () => void;
  closeCreateKBModal: () => void;
  setCreateKBName: (name: string) => void;
  setCreateKBDesc: (desc: string) => void;
  setCreateKBLoading: (v: boolean) => void;

  // Upload Doc
  openUploadDocModal: () => void;
  closeUploadDocModal: () => void;
  setUploadDocLoading: (v: boolean) => void;
  setUploadDocFileList: (files: File[] | ((prev: File[]) => File[])) => void;
  setUploadParentChunkSize: (v: number) => void;
  setUploadChildChunkSize: (v: number) => void;
  setUploadOverlapTokens: (v: number) => void;
  setUploadParentChildIndexing: (v: boolean) => void;
  setUploadAutoKeywordsCount: (v: number) => void;
  setUploadAutoQuestionsCount: (v: number) => void;
  setUploadSeparators: (v: string) => void;
  setUploadChildSeparators: (v: string) => void;
  setUploadStep: (step: 'config' | 'preview') => void;
  setUploadChunkMethod: (method: string) => void;
  setChunkPreviewData: (data: ChunkPreviewResult | null) => void;
  setChunkPreviewLoading: (v: boolean) => void;
  setExpandedChunkIndex: (index: number | null) => void;

  // Reindex
  openReindexModal: (docId: string, kbId: string) => void;
  closeReindexModal: () => void;
  setReindexChunkMethod: (method: string) => void;
  setReindexLoading: (v: boolean) => void;

  /** Reset upload form to defaults */
  resetUploadForm: () => void;
}

const UPLOAD_DEFAULTS = {
  uploadDocFileList: [] as File[],
  uploadParentChunkSize: 1024,
  uploadChildChunkSize: 128,
  uploadOverlapTokens: 20,
  uploadParentChildIndexing: true,
  uploadAutoKeywordsCount: 0,
  uploadAutoQuestionsCount: 0,
  uploadSeparators: '',
  uploadChildSeparators: '',
  uploadStep: 'config' as const,
  uploadChunkMethod: 'structured',
  chunkPreviewData: null,
  chunkPreviewLoading: false,
  expandedChunkIndex: null,
  uploadDocLoading: false,
};

export const useKbStore = create<KBState>((set) => ({
  // Document browsing
  kbDocumentsMap: {},
  kbDocQuery: '',
  activeKbDoc: null,
  kbDocsLoadingId: null,
  kbDocDetailLoadingId: null,
  docDetailTab: 'content',
  docChunks: [],
  docChunksLoading: false,
  chunkSaving: null,

  // Create KB
  createKBModalOpen: false,
  createKBName: '',
  createKBDesc: '',
  createKBLoading: false,

  // Upload Doc
  ...UPLOAD_DEFAULTS,
  uploadDocModalOpen: false,

  // Reindex
  reindexModalOpen: false,
  reindexChunkMethod: 'structured',
  reindexDocId: null,
  reindexKbId: null,
  reindexLoading: false,

  // ── Actions ──
  setKbDocumentsMap: (mapOrUpdater) => {
    if (typeof mapOrUpdater === 'function') {
      set((s) => ({ kbDocumentsMap: mapOrUpdater(s.kbDocumentsMap) }));
    } else {
      set({ kbDocumentsMap: mapOrUpdater });
    }
  },
  updateKbDocuments: (kbId, docs) =>
    set((s) => ({ kbDocumentsMap: { ...s.kbDocumentsMap, [kbId]: docs } })),
  setKbDocQuery: (query) => set({ kbDocQuery: query }),
  setActiveKbDoc: (docOrUpdater) => {
    if (typeof docOrUpdater === 'function') {
      set((s) => ({ activeKbDoc: docOrUpdater(s.activeKbDoc) }));
    } else {
      set({ activeKbDoc: docOrUpdater });
    }
  },
  setKbDocsLoadingId: (id) => set({ kbDocsLoadingId: id }),
  setKbDocDetailLoadingId: (id) => set({ kbDocDetailLoadingId: id }),
  setDocDetailTab: (tab) => set({ docDetailTab: tab }),
  setDocChunks: (chunksOrUpdater) => {
    if (typeof chunksOrUpdater === 'function') {
      set((s) => ({ docChunks: chunksOrUpdater(s.docChunks) }));
    } else {
      set({ docChunks: chunksOrUpdater });
    }
  },
  setDocChunksLoading: (v) => set({ docChunksLoading: v }),
  setChunkSaving: (id) => set({ chunkSaving: id }),

  openCreateKBModal: () => set({ createKBModalOpen: true, createKBName: '', createKBDesc: '' }),
  closeCreateKBModal: () => set({ createKBModalOpen: false, createKBName: '', createKBDesc: '', createKBLoading: false }),
  setCreateKBName: (name) => set({ createKBName: name }),
  setCreateKBDesc: (desc) => set({ createKBDesc: desc }),
  setCreateKBLoading: (v) => set({ createKBLoading: v }),

  openUploadDocModal: () => set({ uploadDocModalOpen: true, ...UPLOAD_DEFAULTS }),
  closeUploadDocModal: () => set({ uploadDocModalOpen: false, ...UPLOAD_DEFAULTS }),
  setUploadDocLoading: (v) => set({ uploadDocLoading: v }),
  setUploadDocFileList: (filesOrUpdater) => {
    if (typeof filesOrUpdater === 'function') {
      set((s) => ({ uploadDocFileList: filesOrUpdater(s.uploadDocFileList) }));
    } else {
      set({ uploadDocFileList: filesOrUpdater });
    }
  },
  setUploadParentChunkSize: (v) => set({ uploadParentChunkSize: v }),
  setUploadChildChunkSize: (v) => set({ uploadChildChunkSize: v }),
  setUploadOverlapTokens: (v) => set({ uploadOverlapTokens: v }),
  setUploadParentChildIndexing: (v) => set({ uploadParentChildIndexing: v }),
  setUploadAutoKeywordsCount: (v) => set({ uploadAutoKeywordsCount: v }),
  setUploadAutoQuestionsCount: (v) => set({ uploadAutoQuestionsCount: v }),
  setUploadSeparators: (v) => set({ uploadSeparators: v }),
  setUploadChildSeparators: (v) => set({ uploadChildSeparators: v }),
  setUploadStep: (step) => set({ uploadStep: step }),
  setUploadChunkMethod: (method) => set({ uploadChunkMethod: method }),
  setChunkPreviewData: (data) => set({ chunkPreviewData: data }),
  setChunkPreviewLoading: (v) => set({ chunkPreviewLoading: v }),
  setExpandedChunkIndex: (index) => set({ expandedChunkIndex: index }),

  openReindexModal: (docId, kbId) => set({
    reindexModalOpen: true,
    reindexDocId: docId,
    reindexKbId: kbId,
    reindexChunkMethod: 'structured',
    reindexLoading: false,
  }),
  closeReindexModal: () => set({
    reindexModalOpen: false,
    reindexDocId: null,
    reindexKbId: null,
    reindexLoading: false,
  }),
  setReindexChunkMethod: (method) => set({ reindexChunkMethod: method }),
  setReindexLoading: (v) => set({ reindexLoading: v }),

  resetUploadForm: () => set(UPLOAD_DEFAULTS),
}));
