import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import {
  Button, Empty, Input, Modal, Pagination, Popconfirm, Radio, Select, Switch, Tag, Typography,
  Upload, Collapse, InputNumber, message,
} from 'antd';
import {
  ArrowLeftOutlined, CloseOutlined, DeleteOutlined, EditOutlined, EyeOutlined,
  InboxOutlined, LoadingOutlined, PlusOutlined, SafetyCertificateOutlined,
  ReloadOutlined, SearchOutlined, StarFilled, ThunderboltOutlined, UploadOutlined,
} from '@ant-design/icons';
import { getFileIconSrc, getFolderIconSrc } from '../../utils/fileIcon';
import { parseSeparators } from '../../utils/separators';
import { useChunkChildrenExpander } from '../../hooks/useChunkChildrenExpander';
import { usePanelHeader } from '../../hooks/usePageConfig';
import { useCatalogStore, useEditionStore, useKbStore } from '../../stores';
import { t } from '../../i18n';
import {
  createKBSpace,
  deleteKBDocument,
  deleteKBSpace,
  getKBChunks,
  getKBChunkChildren,
  getKBDocumentDetail,
  getKBDocuments,
  polishKBDescription,
  previewChunks,
  updateKBSpace,
  updateKBChunk,
  uploadKBDocument,
} from '../../api';
import type { IndexingConfig, KBChunkChild, KBDocumentsResponse } from '../../api';
import type { KBDocument, KBItem } from '../../types';
import { formatDateTime } from '../../utils/date';
import { mdToHtml } from '../../utils/markdown';
import { EASE, staggerStyle } from '../../utils/motionTokens';
import { useStatusFlash } from '../../hooks/useFlash';
import { useAuthStore } from '../../stores/authStore';

type KBTabKey = 'public' | 'private';
const KB_TAB_STORAGE_KEY = 'hugagent_kb_active_tab';

type UploadChunkMethodOption = {
  value: string;
  label: string;
  desc: string;
  recommended?: boolean;
};

const UPLOAD_CHUNK_METHOD_OPTIONS: UploadChunkMethodOption[] = [
  { value: 'structured', label: t('结构感知（按标题和段落）'), desc: t('适合结构清晰的报告、通知、制度文档') },
  { value: 'recursive', label: t('递归分块（多级分隔符）'), desc: t('按文本层级切分，适合通用长文档') },
  { value: 'embedding_semantic', label: t('语义分块（基于嵌入相似度）'), desc: t('更关注语义完整性，适合复杂内容'), recommended: true },
  { value: 'laws', label: t('法律文书'), desc: t('按条款和层级组织，更适合法律法规类文本') },
  { value: 'qa', label: t('问答对'), desc: t('适合 FAQ、客服问答、知识问答数据') },
];

const KB_DOC_PAGE_SIZE = 20;
const EMPTY_DOCS: KBDocument[] = [];

const DOC_FILTERS = [
  { key: 'all', label: t('全部'), countKey: 'total' },
  { key: 'indexed', label: t('已索引'), countKey: 'indexed' },
  { key: 'processing', label: t('索引中'), countKey: 'processing' },
] as const;
type DocStatusFilter = typeof DOC_FILTERS[number]['key'];

function loadActiveKbTab(): KBTabKey {
  if (typeof window === 'undefined') return 'public';
  const raw = window.localStorage.getItem(KB_TAB_STORAGE_KEY);
  return raw === 'private' ? 'private' : 'public';
}

function saveActiveKbTab(tab: KBTabKey) {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(KB_TAB_STORAGE_KEY, tab);
}

function resolveVisibility(item: KBItem): KBTabKey {
  // private = the user's own private library (goes to the "private" Tab); everything else (public / scoped
  // and other shared libraries, including authorized scoped-visibility ones) goes to the "public" Tab —
  // otherwise an authorized scoped library, being is_public=false, would fall into the private Tab and be invisible.
  if (item.visibility === 'private') return 'private';
  if (item.visibility) return 'public';
  return item.is_public ? 'public' : 'private';
}

function parseDocumentTimestamp(rawValue: unknown): number | undefined {
  if (rawValue === null || rawValue === undefined || rawValue === '') return undefined;

  if (typeof rawValue === 'number' && Number.isFinite(rawValue)) {
    return rawValue < 1e12 ? rawValue * 1000 : rawValue;
  }

  if (typeof rawValue === 'string') {
    const trimmed = rawValue.trim();
    if (!trimmed) return undefined;

    if (/^\d+$/.test(trimmed)) {
      const numericValue = Number(trimmed);
      if (!Number.isFinite(numericValue)) return undefined;
      return numericValue < 1e12 ? numericValue * 1000 : numericValue;
    }

    const parsedValue = Date.parse(trimmed);
    return Number.isNaN(parsedValue) ? undefined : parsedValue;
  }

  return undefined;
}

function mapDocument(raw: any): KBDocument {
  const createdAtRaw = raw?.created_at ?? raw?.uploaded_at ?? raw?.createdAt ?? raw?.uploadedAt;
  const createdAt = parseDocumentTimestamp(createdAtRaw);
  const wordCount = typeof raw?.word_count === 'number'
    ? raw.word_count
    : Number.isFinite(Number(raw?.word_count))
      ? Number(raw.word_count)
      : undefined;
  const sizeBytes = typeof raw?.size_bytes === 'number'
    ? raw.size_bytes
    : typeof raw?.size === 'number'
      ? raw.size
      : Number.isFinite(Number(raw?.size_bytes))
        ? Number(raw.size_bytes)
        : Number.isFinite(Number(raw?.size))
          ? Number(raw.size)
          : undefined;

  return {
    id: String(raw?.id ?? raw?.document_id ?? ''),
    title: String(raw?.title ?? raw?.name ?? raw?.filename ?? ''),
    desc: raw?.desc ?? raw?.filename ?? undefined,
    content: typeof raw?.content === 'string' ? raw.content : undefined,
    indexing_status: raw?.indexing_status ?? undefined,
    word_count: wordCount,
    size_bytes: sizeBytes,
    created_at: createdAt && !Number.isNaN(createdAt) ? createdAt : undefined,
  };
}

function formatCount(value?: number, unit = '') {
  if (typeof value !== 'number' || Number.isNaN(value)) return '--';
  return `${value}${unit}`;
}

function formatDocWordCount(doc: KBDocument) {
  return formatCount(doc.word_count, '字');
}

function getDocumentBadge(name: string) {
  return (
    <img
      className="jx-kbDocType"
      src={getFileIconSrc(name)}
      width="20"
      height="20"
      alt=""
      aria-hidden="true"
    />
  );
}

function formatKbDisplayName(name?: string) {
  return name || '';
}

function formatKbSummaryDesc(description?: string) {
  if (!description) return '';
  const [summary] = description.split('规则说明：');
  return summary.trim();
}

/** Filter out technical/internal tags that shouldn't be shown to users. */
function filterDisplayTags(tags?: string[]): string[] {
  if (!Array.isArray(tags)) return [];
  return tags.filter((tag) => {
    if (!tag || typeof tag !== 'string') return false;
    // Hide provider-internal paths such as "vendor/protocol/...".
    if (tag.includes('/')) return false;
    // Hide technical quality flags
    if (tag === 'high_quality' || tag === 'economy') return false;
    return true;
  });
}

export function CatalogPanel() {
  const isCE = useEditionStore((s) => s.edition === 'ce');
  const { title: kbTitle, subtitle: kbSubtitle } = usePanelHeader('kb', {
    title: '知识库',
    subtitle: '浏览知识库、查看文档列表，并支持文档内检索。',
  });
  const {
    catalog, catalogLoading,
    panelEntryNonce,
    manageQuery, setManageQuery,
    selectedId, setSelectedId,
    fetchCatalog, toggleItem,
  } = useCatalogStore();

  // There are two kinds of create-knowledge-base permission: private (self only) / public (visible to everyone by default, can be further restricted by authorization).
  const canCreatePrivateKb = useAuthStore((s) => s.authUser?.can_create_private_kb === true);
  const canCreatePublicKb = useAuthStore(
    (s) => !isCE && s.authUser?.can_create_public_kb === true,
  );
  const canCreateKb = canCreatePrivateKb || canCreatePublicKb;

  const {
    kbDocQuery, setKbDocQuery,
    activeKbDoc, setActiveKbDoc,
    kbDocumentsMap, setKbDocumentsMap,
    kbDocsLoadingId, setKbDocsLoadingId,
    kbDocDetailLoadingId, setKbDocDetailLoadingId,
    uploadDocModalOpen, uploadDocLoading,
    uploadDocFileList, setUploadDocFileList,
    openUploadDocModal, closeUploadDocModal,
    setUploadDocLoading,
    uploadParentChunkSize, setUploadParentChunkSize,
    uploadChildChunkSize, setUploadChildChunkSize,
    uploadOverlapTokens, setUploadOverlapTokens,
    uploadParentChildIndexing, setUploadParentChildIndexing,
    uploadAutoKeywordsCount, setUploadAutoKeywordsCount,
    uploadAutoQuestionsCount, setUploadAutoQuestionsCount,
    uploadSeparators, setUploadSeparators,
    uploadChildSeparators, setUploadChildSeparators,
    uploadStep, setUploadStep,
    uploadChunkMethod, setUploadChunkMethod,
    chunkPreviewData, setChunkPreviewData,
    chunkPreviewLoading, setChunkPreviewLoading,
    expandedChunkIndex, setExpandedChunkIndex,
    openReindexModal,
    docDetailTab, setDocDetailTab,
    docChunks, setDocChunks,
    docChunksLoading, setDocChunksLoading,
    chunkSaving, setChunkSaving,
  } = useKbStore();

  const [activeTab, setActiveTab] = useState<KBTabKey>(() => (
    isCE ? 'private' : loadActiveKbTab()
  ));
  const [detailDescExpanded, setDetailDescExpanded] = useState(false);
  const [detailDescOverflow, setDetailDescOverflow] = useState(false);
  const [kbDocPage, setKbDocPage] = useState(1);
  const [kbDocTotal, setKbDocTotal] = useState(0);
  const [docStatusFilter, setDocStatusFilter] = useState<DocStatusFilter>('all');
  const [kbEditorOpen, setKbEditorOpen] = useState(false);
  const [kbEditorMode, setKbEditorMode] = useState<'create' | 'edit'>('create');
  const [kbEditorName, setKbEditorName] = useState('');
  const [kbEditorDesc, setKbEditorDesc] = useState('');
  const [kbEditorLoading, setKbEditorLoading] = useState(false);
  const [kbEditorPolishing, setKbEditorPolishing] = useState(false);
  const [kbEditorVisibility, setKbEditorVisibility] = useState<'private' | 'public'>('private');
  const detailDescRef = useRef<HTMLParagraphElement | null>(null);
  const tabsRef = useRef<HTMLDivElement | null>(null);
  const tabButtonRefs = useRef<Partial<Record<KBTabKey, HTMLButtonElement | null>>>({});
  const [tabIndicatorStyle, setTabIndicatorStyle] = useState<{ left: number; width: number; ready: boolean }>({
    left: 0,
    width: 0,
    ready: false,
  });
  // Set of chunk_id currently in "content editing" state (entered by clicking "edit"; the content is rendered as a TextArea)
  const [editingChunks, setEditingChunks] = useState<Set<string>>(new Set());
  const toggleChunkEditing = useCallback((chunkId: string) => {
    setEditingChunks((prev) => {
      const next = new Set(prev);
      if (next.has(chunkId)) next.delete(chunkId);
      else next.add(chunkId);
      return next;
    });
  }, []);

  const kbItems = useMemo(
    () => (catalog.kb as KBItem[]).filter((item) => !isCE || resolveVisibility(item) === 'private'),
    [catalog.kb, isCE],
  );

  useEffect(() => {
    if (isCE && activeTab !== 'private') setActiveTab('private');
  }, [activeTab, isCE]);

  const counts = useMemo(() => {
    let publicCount = 0;
    let privateCount = 0;
    kbItems.forEach((item) => {
      if (resolveVisibility(item) === 'public') publicCount += 1;
      else privateCount += 1;
    });
    return { public: publicCount, private: privateCount };
  }, [kbItems]);

  const selectedItem = useMemo(
    () => kbItems.find((item) => item.id === selectedId) || null,
    [kbItems, selectedId],
  );

  // Parent→child expansion (lazy-load + cache), shares the same hook as the public library
  const fetchChunkChildren = useCallback(
    (parentId: string) => (selectedId ? getKBChunkChildren(selectedId, parentId) : Promise.resolve([])),
    [selectedId],
  );
  const {
    childrenMap: chunkChildrenMap,
    expandedParents: expandedChunkParents,
    loadingParents: chunkChildrenLoading,
    toggle: toggleChunkChildren,
    reset: resetChunkChildren,
  } = useChunkChildrenExpander<KBChunkChild>(fetchChunkChildren);

  // Whether editing chunk tags/questions is allowed: decided by the authorization capability bit editable (true for owners / authorized admins;
  // ordinary public and externally managed libraries have editable=false, preview-only). No longer using is_public as a blanket rule — otherwise
  // a shared library the user is authorized to manage would also be wrongly treated as read-only.
  const canEditChunks = !!selectedItem && !!selectedItem.editable;

  useEffect(() => {
    if (selectedItem) {
      setActiveTab(resolveVisibility(selectedItem));
    }
  }, [selectedItem]);

  useEffect(() => {
    setKbDocPage(1);
    setKbDocTotal(selectedItem?.document_count || 0);
  }, [selectedItem?.id, selectedItem?.document_count]);

  useEffect(() => {
    saveActiveKbTab(activeTab);
  }, [activeTab]);

  useEffect(() => {
    const updateIndicator = () => {
      const tabsEl = tabsRef.current;
      const activeEl = tabButtonRefs.current[activeTab];
      if (!tabsEl || !activeEl) return;
      const tabsRect = tabsEl.getBoundingClientRect();
      const activeRect = activeEl.getBoundingClientRect();
      setTabIndicatorStyle({
        left: activeRect.left - tabsRect.left,
        width: activeRect.width,
        ready: true,
      });
    };

    updateIndicator();
    window.addEventListener('resize', updateIndicator);
    return () => window.removeEventListener('resize', updateIndicator);
  }, [activeTab, counts.public, counts.private]);

  useEffect(() => {
    setDetailDescExpanded(false);
  }, [selectedItem?.id]);

  useEffect(() => {
    const measureOverflow = () => {
      const el = detailDescRef.current;
      if (!el || detailDescExpanded) return;
      setDetailDescOverflow(el.scrollWidth > el.clientWidth + 1);
    };

    measureOverflow();
    window.addEventListener('resize', measureOverflow);
    return () => window.removeEventListener('resize', measureOverflow);
  }, [selectedItem?.desc, detailDescExpanded]);

  useEffect(() => {
    if (selectedId && !selectedItem) {
      setSelectedId(null);
    }
  }, [selectedId, selectedItem, setSelectedId]);

  useEffect(() => {
    setDocStatusFilter('all');
  }, [selectedId]);

  // Only compare the fields that drive the UI (id + indexing_status). When the newly fetched array is equivalent to the old one, skip
  // the store write — Zustand's set does not short-circuit on reference equality, and would notify all subscribers to re-render.
  const docsShallowEqual = useCallback(
    (a: KBDocument[] | undefined, b: KBDocument[]): boolean => {
      if (!a || a.length !== b.length) return false;
      for (let i = 0; i < a.length; i++) {
        if (a[i].id !== b[i].id || a[i].indexing_status !== b[i].indexing_status) return false;
      }
      return true;
    },
    [],
  );

  const applyDocumentsResult = useCallback(
    (kbId: string, items: any[], total: number) => {
      const mapped = items.map(mapDocument);
      const prev = useKbStore.getState().kbDocumentsMap[kbId];
      if (!docsShallowEqual(prev, mapped)) {
        setKbDocumentsMap((p) => ({ ...p, [kbId]: mapped }));
      }
      setKbDocTotal(total);
    },
    [docsShallowEqual, setKbDocumentsMap],
  );

  useEffect(() => {
    if (!selectedId) return;
    const kbId = selectedId;
    setKbDocsLoadingId(kbId);
    void (async () => {
      try {
        const result: KBDocumentsResponse = await getKBDocuments(kbId, kbDocPage, KB_DOC_PAGE_SIZE);
        const totalPages = result.total > 0 ? Math.ceil(result.total / result.page_size) : 0;
        if (result.total > 0 && totalPages > 0 && kbDocPage > totalPages) {
          setKbDocPage(totalPages);
          return;
        }
        applyDocumentsResult(kbId, result.items, result.total);
      } catch {
        applyDocumentsResult(kbId, [], 0);
      } finally {
        setKbDocsLoadingId(null);
      }
    })();
  }, [selectedId, kbDocPage, applyDocumentsResult, setKbDocsLoadingId]);

  const filteredLibraries = useMemo(() => {
    const query = manageQuery.trim().toLowerCase();
    return kbItems
      .filter((item) => resolveVisibility(item) === activeTab)
      .filter((item) => {
        if (!query) return true;
        return `${item.name} ${item.desc} ${item.id}`.toLowerCase().includes(query);
      })
      .sort((a, b) => {
        const pinDelta = Number(Boolean(b.pinned)) - Number(Boolean(a.pinned));
        if (pinDelta !== 0) return pinDelta;
        return (a.name || '').localeCompare(b.name || '', 'zh-CN');
      });
  }, [kbItems, activeTab, manageQuery]);

  const documents = useMemo(
    () => (selectedItem ? (kbDocumentsMap[selectedItem.id] ?? EMPTY_DOCS) : EMPTY_DOCS),
    [selectedItem, kbDocumentsMap],
  );

  // "indexing→done" row highlight: the animation is bound to the status diff (the previous round's indexing_status snapshot),
  // not to render — so the 5s polling that replaces the whole array won't falsely trigger it.
  const docFlashIds = useStatusFlash(
    documents,
    (doc) => doc.id,
    (doc) => doc.indexing_status ?? '',
    (prev) => prev === 'processing',
    1500,
  );

  const isIndexedStatus = (status: string | undefined) =>
    status !== 'processing' && status !== 'failed';

  // Depend on a boolean rather than the array: the reference changes after each polling write-back, but as long as there is still processing, the effect won't restart.
  const hasProcessingDoc = useMemo(
    () => documents.some((doc) => doc.indexing_status === 'processing'),
    [documents],
  );

  useEffect(() => {
    if (!selectedId || !hasProcessingDoc) return;
    const kbId = selectedId;
    const currentPage = kbDocPage;
    const timer = window.setInterval(async () => {
      try {
        const result = await getKBDocuments(kbId, currentPage, KB_DOC_PAGE_SIZE);
        applyDocumentsResult(kbId, result.items, result.total);
      } catch (err) {
        console.warn('KB 文档轮询失败', err);
      }
    }, 5000);
    return () => window.clearInterval(timer);
  }, [selectedId, kbDocPage, hasProcessingDoc, applyDocumentsResult]);

  const filteredDocuments = useMemo(() => {
    const query = kbDocQuery.trim().toLowerCase();
    return documents.filter((doc) => {
      if (docStatusFilter === 'processing' && doc.indexing_status !== 'processing') return false;
      if (docStatusFilter === 'indexed' && !isIndexedStatus(doc.indexing_status)) return false;
      if (!query) return true;
      return `${doc.title} ${doc.desc || ''} ${doc.content || ''}`.toLowerCase().includes(query);
    });
  }, [documents, kbDocQuery, docStatusFilter]);

  const detailStats = useMemo(() => {
    const total = kbDocTotal || selectedItem?.document_count || 0;
    const indexed = documents.filter((doc) => isIndexedStatus(doc.indexing_status)).length;
    const processing = documents.filter((doc) => doc.indexing_status === 'processing').length;
    return { total, indexed, processing };
  }, [documents, kbDocTotal, selectedItem]);

  const docEmptyDescription = (() => {
    if (documents.length === 0) return t('该知识库暂无文档');
    if (kbDocQuery) return t('没有匹配的文档');
    if (docStatusFilter === 'processing') return t('当前没有索引中的文档');
    if (docStatusFilter === 'indexed') return t('当前没有已索引的文档');
    return t('没有匹配的文档');
  })();

  const refreshCatalog = async () => {
    await fetchCatalog();
  };

  const closeKbEditor = () => {
    setKbEditorOpen(false);
    setKbEditorMode('create');
    setKbEditorName('');
    setKbEditorDesc('');
    setKbEditorLoading(false);
    setKbEditorPolishing(false);
  };

  const openCreateKbEditor = () => {
    setKbEditorMode('create');
    setKbEditorName('');
    setKbEditorDesc('');
    // Default to whichever one is permitted; if both are, default to private
    setKbEditorVisibility(canCreatePrivateKb ? 'private' : 'public');
    setKbEditorOpen(true);
  };

  const openEditKbEditor = (item: KBItem) => {
    setKbEditorMode('edit');
    setKbEditorName(item.name || '');
    setKbEditorDesc(item.desc || '');
    setKbEditorOpen(true);
  };

  const handleKbEditorSubmit = async () => {
    const name = kbEditorName.trim();
    const description = kbEditorDesc.trim();

    if (!name) {
      message.warning(t('请输入知识库名称'));
      return;
    }

    setKbEditorLoading(true);
    try {
      if (kbEditorMode === 'create') {
        await createKBSpace(name, description || undefined, undefined, undefined, kbEditorVisibility);
        message.success(t('知识库已创建'));
      } else if (selectedItem) {
        await updateKBSpace(selectedItem.id, {
          name,
          description: description || '',
        });
        message.success(t('知识库信息已更新'));
      }
      await refreshCatalog();
      closeKbEditor();
    } catch (err: any) {
      message.error(err?.message || (kbEditorMode === 'create' ? t('创建失败') : t('更新失败')));
    } finally {
      setKbEditorLoading(false);
    }
  };

  const handlePolishKbDescription = async () => {
    const name = kbEditorName.trim();
    if (!name) {
      message.warning(t('请先输入知识库名称'));
      return;
    }

    setKbEditorPolishing(true);
    try {
      const polished = await polishKBDescription(name, kbEditorDesc.trim() || undefined);
      if (!polished) {
        message.warning(t('未生成知识库简介，请稍后重试'));
        return;
      }
      setKbEditorDesc(polished);
      message.success(t('已生成知识库简介'));
    } catch (err: any) {
      message.error(err?.message || t('生成知识库简介失败'));
    } finally {
      setKbEditorPolishing(false);
    }
  };

  const refreshSelectedLibrary = async () => {
    if (!selectedItem || kbDocsLoadingId === selectedItem.id) return;
    setKbDocsLoadingId(selectedItem.id);
    try {
      const result = await getKBDocuments(selectedItem.id, kbDocPage, KB_DOC_PAGE_SIZE);
      applyDocumentsResult(selectedItem.id, result.items, result.total);
      await refreshCatalog();
    } catch (err: any) {
      message.error(err?.message || t('刷新文档列表失败'));
    } finally {
      setKbDocsLoadingId(null);
    }
  };

  const openKbDocumentDetail = async (doc: KBDocument) => {
    setActiveKbDoc(doc);
    if (!selectedId || doc.content) return;
    if (kbDocDetailLoadingId === doc.id) return;

    setKbDocDetailLoadingId(doc.id);
    try {
      const detail = await getKBDocumentDetail(selectedId, doc.id);
      const detailedDoc: KBDocument = {
        ...doc,
        title: detail.title || doc.title,
        desc: detail.desc ?? doc.desc,
        content: detail.content,
      };
      setActiveKbDoc((prev) => (prev && prev.id === doc.id ? { ...prev, ...detailedDoc } : prev));
      setKbDocumentsMap((prev) => ({
        ...prev,
        [selectedId]: (prev[selectedId] || []).map((item) => (item.id === doc.id ? { ...item, ...detailedDoc } : item)),
      }));
    } catch {
      message.error(t('加载文档详情失败'));
    } finally {
      setKbDocDetailLoadingId(null);
    }
  };

  const handleUpload = async (isPreview = false) => {
    if (uploadDocFileList.length === 0) {
      message.warning(t('请选择文件'));
      return;
    }
    if (!selectedItem) return;

    if (isPreview) {
      setChunkPreviewLoading(true);
      try {
        const result = await previewChunks(
          uploadDocFileList[0], uploadChunkMethod, uploadParentChunkSize,
          uploadChildChunkSize, uploadOverlapTokens, uploadParentChildIndexing,
          parseSeparators(uploadSeparators),
          uploadParentChildIndexing ? parseSeparators(uploadChildSeparators) : undefined,
        );
        setChunkPreviewData(result);
        setUploadStep('preview');
        setExpandedChunkIndex(null);
      } catch (err: any) {
        message.error(err.message || t('预览失败'));
      } finally {
        setChunkPreviewLoading(false);
      }
      return;
    }

    setUploadDocLoading(true);
    try {
      const _separators = parseSeparators(uploadSeparators);
      const _childSeparators = parseSeparators(uploadChildSeparators);
      const idxCfg: IndexingConfig = {
        parent_chunk_size: uploadParentChunkSize,
        child_chunk_size: uploadChildChunkSize,
        overlap_tokens: uploadOverlapTokens,
        parent_child_indexing: uploadParentChildIndexing,
        auto_keywords_count: uploadAutoKeywordsCount,
        auto_questions_count: uploadAutoQuestionsCount,
        ...(_separators.length ? { separators: _separators } : {}),
        ...(_childSeparators.length && uploadParentChildIndexing ? { child_separators: _childSeparators } : {}),
      };
      for (const file of uploadDocFileList) {
        await uploadKBDocument(selectedItem.id, file, undefined, idxCfg, uploadChunkMethod);
      }
      setKbDocPage(1);
      const result = await getKBDocuments(selectedItem.id, 1, KB_DOC_PAGE_SIZE);
      applyDocumentsResult(selectedItem.id, result.items, result.total);
      await refreshCatalog();
      closeUploadDocModal();
      message.success(`${uploadDocFileList.length} ${t('个文档已上传，正在后台索引')}`);
    } catch (err: any) {
      message.error(err.message || t('上传失败'));
    } finally {
      setUploadDocLoading(false);
    }
  };

  const emptyLibraries = !catalogLoading && filteredLibraries.length === 0;
  const isPrivateLibrary = selectedItem ? resolveVisibility(selectedItem) === 'private' : false;
  // Whether the user has any management capability over this library (owner / authorized edit|admin). Once authorized, a shared library can also be managed on the user side,
  // no longer using the blanket rule "only private libraries can be managed". Each button is then disabled individually by uploadable/editable/deletable.
  const canManage = !!selectedItem
    && (!!selectedItem.uploadable || !!selectedItem.editable || !!selectedItem.deletable);
  const selectedItemDisplayName = formatKbDisplayName(selectedItem?.name);
  const currentDocCount = kbDocTotal || selectedItem?.document_count || 0;
  const libraryLoadingCards = Array.from({ length: 10 }, (_, index) => index);
  const docLoadingRows = Array.from({ length: 10 }, (_, index) => index);

  return (
    <>
      <div className="jx-kbView">
        {!selectedItem ? (
          <>
            <div className="jx-agentPage-header">
              <div>
                <div className="jx-agentPage-title">{kbTitle}</div>
                {kbSubtitle ? <div className="jx-agentPage-subtitle">{kbSubtitle}</div> : null}
              </div>
            </div>
            <section className="jx-kbTabsWrap">
              <div className="jx-kbTabs" ref={tabsRef}>
                {((isCE ? ['private'] : ['public', 'private']) as KBTabKey[]).map((tab) => {
                  const tabLabel = tab === 'public' ? t('公共知识库') : t('私有知识库');
                  return (
                    <button
                      key={tab}
                      ref={(el) => {
                        tabButtonRefs.current[tab] = el;
                      }}
                      className={`jx-kbTab${activeTab === tab ? ' active' : ''}`}
                      onClick={() => {
                        setActiveTab(tab);
                        setManageQuery('');
                      }}
                    >
                      <span>{tabLabel}</span>
                      <span className="jx-kbTabCount">{counts[tab]}</span>
                    </button>
                  );
                })}
                <span
                  className={`jx-kbTabIndicator${tabIndicatorStyle.ready ? ' is-ready' : ''}`}
                  style={{ transform: `translateX(${tabIndicatorStyle.left}px)`, width: tabIndicatorStyle.width }}
                  aria-hidden="true"
                />
              </div>
            </section>

            <section className="jx-kbToolbar">
              <Input
                allowClear
                value={manageQuery}
                onChange={(e) => setManageQuery(e.target.value)}
                prefix={<SearchOutlined />}
                placeholder={activeTab === 'public' ? t('搜索公共知识库') : t('搜索私有知识库')}
                className="jx-kbToolbarSearch"
              />
              <div className="jx-kbToolbarMeta">
                <span>
                  {t('共 {n} 个知识库', { n: counts[activeTab] })}
                  {activeTab === 'public' ? t(' · 由管理员统一维护') : t(' · 仅自己可见与维护')}
                </span>
                <Button icon={<ReloadOutlined />} onClick={() => void refreshCatalog()} disabled={catalogLoading}>
                  {t('刷新')}
                </Button>
                {canCreateKb && (
                  <Button type="primary" icon={<PlusOutlined />} onClick={openCreateKbEditor}>
                    {t('新增知识库')}
                  </Button>
                )}
              </div>
            </section>

            {/* The container key controls card stagger replay: replays on entering the panel / switching Tab, but not on the optimistic update of the enable toggle.
                The empty state uses its own jx-anim-fadeIn, not attached to stagger, to avoid being overridden by the primitive's fadeInUp */}
            <section
              className={`jx-kbLibraryGrid${catalogLoading || !emptyLibraries ? ' jx-anim-stagger' : ''}`}
              style={{ '--stagger-step': '30ms' } as React.CSSProperties}
              key={`kb-${panelEntryNonce}-${activeTab}`}
            >
              {catalogLoading ? (
                libraryLoadingCards.map((item) => (
                  <div key={item} className="jx-kbLibraryCard jx-kbLibraryCardSkeleton" aria-hidden="true">
                    <div className="jx-kbLibraryCardTop">
                      <div className="jx-skeletonBlock jx-kbSkIcon" />
                      <div className="jx-kbLibraryMain">
                        <div className="jx-kbLibraryTitleRow">
                          <div className="jx-skeletonBlock jx-kbSkTitle" />
                          <div className="jx-skeletonBlock jx-kbSkArrow" />
                        </div>
                        <div className="jx-skeletonBlock jx-kbSkDesc" />
                      </div>
                    </div>
                    <div className="jx-kbLibraryTags">
                      <div className="jx-skeletonBlock jx-kbSkTag" />
                      <div className="jx-skeletonBlock jx-kbSkTag" />
                      <div className="jx-skeletonBlock jx-kbSkMeta" />
                    </div>
                  </div>
                ))
              ) : emptyLibraries ? (
                <div className="jx-kbLibraryEmpty jx-anim-fadeIn">
                  <Empty
                    description={activeTab === 'public' ? t('暂无公共知识库') : t('暂无私有知识库')}
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                  >
                    {canCreateKb && (
                      <Button type="primary" onClick={openCreateKbEditor}>
                        {t('创建第一个知识库')}
                      </Button>
                    )}
                  </Empty>
                </div>
              ) : (
                filteredLibraries.map((item, idx) => {
                  const visibility = resolveVisibility(item);
                  return (
                    <button
                      key={item.id}
                      type="button"
                      className="jx-kbLibraryCard"
                      style={staggerStyle(idx)}
                      onClick={() => {
                        setSelectedId(item.id);
                        setKbDocQuery('');
                      }}
                    >
                      <div className="jx-kbLibraryCardTop">
                        <div className="jx-kbLibraryIcon"><img src={getFolderIconSrc()} width="44" height="44" alt="" aria-hidden="true" /></div>
                        <div className="jx-kbLibraryMain">
                        <div className="jx-kbLibraryTitleRow">
                            <h3 className="jx-kbLibraryTitle">{formatKbDisplayName(item.name)}</h3>
                          </div>
                          <p className="jx-kbLibraryDesc">{formatKbSummaryDesc(item.desc) || t('暂无知识库说明')}</p>
                        </div>
                      </div>
                      <div className="jx-kbLibraryTags">
                        <span className="jx-kbPill jx-kbPill-blue">{visibility === 'public' ? t('公共') : t('私有')}</span>
                        <Tag
                          className="jx-kbEnabledTag"
                          style={item.enabled
                            ? { background: '#DBE9FF', color: '#126DFF', border: 'none' }
                            : { background: '#F5F6F7', color: '#B3B3B3', border: 'none' }
                          }
                        >
                          {item.enabled ? t('已启用') : t('未启用')}
                        </Tag>
                        {filterDisplayTags(item.tags).map((tag) => (
                          <span
                            key={`${item.id}-${tag}`}
                            className={`jx-kbPill${tag === '系统托管' ? ' jx-kbPill-orange' : ''}`}
                          >
                            {t(tag)}
                          </span>
                        ))}
                        <span className="jx-kbLibraryDocCount">{t('共{n}个文档', { n: formatCount(item.document_count) })}</span>
                      </div>
                    </button>
                  );
                })
              )}
            </section>
          </>
        ) : (
          <>
            <section className="jx-kbDetailHeader">
              <div className="jx-kbDetailHeaderMain">
                <Button
                  icon={<ArrowLeftOutlined />}
                  className="jx-kbBackBtn"
                  type="text"
                  aria-label={t('返回列表')}
                  title={t('返回列表')}
                  onClick={() => {
                    setSelectedId(null);
                    setKbDocQuery('');
                  }}
                />
                <div className="jx-kbDetailDivider" />
                <div className="jx-kbDetailIcon"><img src={getFolderIconSrc()} width="24" height="24" alt="" aria-hidden="true" /></div>
                <div className="jx-kbDetailIntro">
                  <div className="jx-kbDetailTitleRow">
                    <h2 className="jx-kbDetailTitle">{selectedItemDisplayName}</h2>
                    <span className="jx-kbPill jx-kbPill-blue">{isPrivateLibrary ? t('私有') : t('公共')}</span>
                    <Tag
                      className="jx-kbEnabledTag"
                      style={selectedItem.enabled
                        ? { background: '#DBE9FF', color: '#126DFF', border: 'none' }
                        : { background: '#F5F6F7', color: '#B3B3B3', border: 'none' }
                      }
                    >
                      {selectedItem.enabled ? t('已启用') : t('未启用')}
                    </Tag>
                    {filterDisplayTags(selectedItem.tags).map((tag) => (
                      <span
                        key={`${selectedItem.id}-${tag}`}
                        className={`jx-kbPill${tag === '系统托管' ? ' jx-kbPill-orange' : ''}`}
                      >
                        {t(tag)}
                      </span>
                    ))}
                  </div>
                  <div className="jx-kbDetailDescRow">
                    <p
                      ref={detailDescRef}
                      className={`jx-kbDetailDesc${detailDescExpanded ? ' is-expanded' : ''}`}
                    >
                      {formatKbSummaryDesc(selectedItem.desc) || t('暂无知识库说明')}
                    </p>
                    {detailDescOverflow && (
                      <Button
                        type="link"
                        className="jx-kbDetailDescToggle"
                        onClick={() => setDetailDescExpanded((prev) => !prev)}
                      >
                        {detailDescExpanded ? '收起' : '展开'}
                      </Button>
                    )}
                  </div>
                </div>
              </div>
              <div className="jx-kbDetailActions">
                <div className="jx-kbEnableRow">
                  <span className="jx-kbEnableLabel">{t('启用')}</span>
                  <Switch
                    checked={selectedItem.enabled}
                    onChange={(checked) => void toggleItem('kb', selectedItem.id, checked)}
                  />
                </div>
                {!canManage ? (
                  <span className="jx-kbDetailBadge"><SafetyCertificateOutlined /> {t('由管理员维护')}</span>
                ) : (
                  <>
                    <Button
                      icon={<UploadOutlined />}
                      type="primary"
                      onClick={() => openUploadDocModal()}
                      disabled={!selectedItem.uploadable}
                    >
                      {t('上传文档')}
                    </Button>
                    <div className="jx-kbDetailIconGroup">
                      <Button
                        type="text"
                        className="jx-kbEditIconBtn"
                        icon={<EditOutlined />}
                        aria-label={t('编辑知识库')}
                        title={t('编辑知识库')}
                        disabled={!selectedItem.editable}
                        onClick={() => selectedItem && openEditKbEditor(selectedItem)}
                      />
                      <Popconfirm
                        title={t('确定删除此知识库？')}
                        description={t('删除后该知识库及其所有文档将不可恢复。')}
                        okText={t('删除')}
                        cancelText={t('取消')}
                        okButtonProps={{ danger: true }}
                        disabled={!selectedItem.deletable}
                        onConfirm={async () => {
                          try {
                            await deleteKBSpace(selectedItem.id);
                            message.success(t('知识库已删除'));
                            setSelectedId(null);
                            await refreshCatalog();
                          } catch (err: any) {
                            message.error(err.message || t('删除失败'));
                          }
                        }}
                      >
                        <Button
                          type="text"
                          className="jx-kbDeleteIconBtn"
                          icon={<DeleteOutlined />}
                          aria-label={t('删除知识库')}
                          title={t('删除知识库')}
                          disabled={!selectedItem.deletable}
                        />
                      </Popconfirm>
                    </div>
                  </>
                )}
              </div>
            </section>

            <section className="jx-kbDocPanel">
              <div className="jx-kbDocPanelHeader">
                <div className="jx-kbDocFilterTabs" role="tablist" aria-label={t('文档状态筛选')}>
                  {DOC_FILTERS.map(({ key, label, countKey }) => {
                    const active = docStatusFilter === key;
                    return (
                      <button
                        key={key}
                        type="button"
                        role="tab"
                        aria-selected={active}
                        className={`jx-kbDocFilterTab${active ? ' is-active' : ''}`}
                        onClick={() => setDocStatusFilter(key)}
                      >
                        <span className="jx-kbDocFilterTabLabel">{label}</span>
                        <span className="jx-kbDocFilterTabCount">{detailStats[countKey]}</span>
                      </button>
                    );
                  })}
                </div>
                <div className="jx-kbDocPanelTools">
                  <Input
                    allowClear
                    value={kbDocQuery}
                    onChange={(e) => setKbDocQuery(e.target.value)}
                    prefix={<SearchOutlined />}
                    placeholder={t('搜索文档...')}
                    className="jx-kbDocSearch"
                  />
                  <Button
                    icon={<ReloadOutlined />}
                    onClick={() => void refreshSelectedLibrary()}
                    disabled={kbDocsLoadingId === selectedItem.id}
                  >
                    {t('刷新')}
                  </Button>
                </div>
              </div>

              <div className="jx-kbDocTable">
                <div className="jx-kbDocTableHead">
                  <div>{t('文件名')}</div>
                  <div>{t('字符数')}</div>
                  <div>{t('上传时间')}</div>
                  <div>{t('状态')}</div>
                  <div>{t('操作')}</div>
                </div>

                {kbDocsLoadingId === selectedItem.id ? (
                  <div className="jx-kbDocLoadingWrap" aria-hidden="true">
                    {docLoadingRows.map((item) => (
                      <div key={item} className="jx-kbDocRow jx-kbDocRowSkeleton">
                        <div className="jx-kbDocNameCell">
                          <div className="jx-skeletonBlock jx-kbSkDocType" />
                          <div className="jx-kbDocNameMain">
                            <div className="jx-skeletonBlock jx-kbSkDocName" />
                            <div className="jx-skeletonBlock jx-kbSkDocDesc" />
                          </div>
                        </div>
                        <div className="jx-skeletonBlock jx-kbSkDocMeta" />
                        <div className="jx-skeletonBlock jx-kbSkDocMeta" />
                        <div className="jx-skeletonBlock jx-kbSkDocStatus" />
                        <div className="jx-kbDocActions">
                          <div className="jx-skeletonBlock jx-kbSkDocAction" />
                        </div>
                      </div>
                    ))}
                  </div>
                ) : filteredDocuments.length === 0 ? (
                  <div className="jx-kbDocEmpty jx-anim-fadeIn">
                    <Empty
                      description={docEmptyDescription}
                      image={Empty.PRESENTED_IMAGE_SIMPLE}
                    >
                      {selectedItem.uploadable && documents.length === 0 && (
                        <Button type="primary" onClick={() => openUploadDocModal()}>
                          {t('上传第一份文档')}
                        </Button>
                      )}
                    </Empty>
                  </div>
                ) : (
                  /* skeleton→content handoff fade-in; wrapper key=library id, the 5s polling write-back does not replay */
                  <div className="jx-kbDocListWrap jx-anim-fadeIn" key={`docs-${selectedItem.id}`}>
                  {filteredDocuments.map((doc) => (
                    <div key={doc.id} className={`jx-kbDocRow${docFlashIds.has(doc.id) ? ' jx-anim-flash' : ''}`}>
                      <div className="jx-kbDocNameCell">
                        {getDocumentBadge(doc.title || doc.id)}
                        <div className="jx-kbDocNameMain">
                          <div className="jx-kbDocName">{doc.title || doc.id}</div>
                        </div>
                      </div>
                      <div className="jx-kbDocCellMuted">{formatDocWordCount(doc)}</div>
                      <div className="jx-kbDocCellMuted">{formatDateTime(doc.created_at)}</div>
                      <div>
                        {doc.indexing_status === 'processing' ? (
                          <span className="jx-kbStatusPill jx-kbStatusPill-processing">
                            <LoadingOutlined /> {t('索引中')}
                          </span>
                        ) : doc.indexing_status === 'failed' ? (
                          <span className="jx-kbStatusPill jx-kbStatusPill-failed">{t('索引失败')}</span>
                        ) : (
                          <span className="jx-kbStatusPill jx-kbStatusPill-success">{t('索引完成')}</span>
                        )}
                      </div>
                      <div className="jx-kbDocActions">
                        <span className="jx-kbDocActionSlot">
                          {selectedItem.editable && doc.indexing_status === 'failed' ? (
                            <Button
                              type="text"
                              icon={<ThunderboltOutlined />}
                              onClick={() => openReindexModal(doc.id, selectedItem.id)}
                              aria-label={t('重新索引文档')}
                              title={t('重新索引文档')}
                            />
                          ) : (
                            <span className="jx-kbDocActionPlaceholder" aria-hidden="true" />
                          )}
                        </span>
                        <span className="jx-kbDocActionSlot">
                          <Button
                            type="text"
                            icon={<EyeOutlined />}
                            onClick={() => void openKbDocumentDetail(doc)}
                            aria-label={t('查看索引分块情况')}
                            title={t('查看索引分块情况')}
                          />
                        </span>
                        <span className="jx-kbDocActionSlot">
                          {selectedItem.deletable ? (
                            <Popconfirm
                              title={t('确定删除此文档？')}
                              okText={t('删除')}
                              cancelText={t('取消')}
                              okButtonProps={{ danger: true }}
                              onConfirm={async () => {
                                try {
                                  await deleteKBDocument(selectedItem.id, doc.id);
                                  const nextTotal = Math.max(0, currentDocCount - 1);
                                  const nextTotalPages = nextTotal > 0 ? Math.ceil(nextTotal / KB_DOC_PAGE_SIZE) : 1;
                                  if (kbDocPage > nextTotalPages) {
                                    setKbDocPage(nextTotalPages);
                                  } else {
                                    await refreshSelectedLibrary();
                                  }
                                  await refreshCatalog();
                                  message.success(t('文档已删除'));
                                } catch (err: any) {
                                  message.error(err.message || t('删除失败'));
                                }
                              }}
                            >
                              <Button
                                type="text"
                                danger
                                icon={<DeleteOutlined />}
                                aria-label={t('删除文档')}
                                title={t('删除文档')}
                              />
                            </Popconfirm>
                          ) : (
                            <span className="jx-kbDocActionPlaceholder" aria-hidden="true" />
                          )}
                        </span>
                      </div>
                    </div>
                  ))}
                  </div>
                )}
              </div>
              {!kbDocsLoadingId && currentDocCount > 0 && (
                <div className="jx-kbDocPagination">
                  <Pagination
                    className="jx-kbPager"
                    current={kbDocPage}
                    pageSize={KB_DOC_PAGE_SIZE}
                    total={currentDocCount}
                    showSizeChanger={false}
                    showTotal={(total) => t('共 {n} 条', { n: total })}
                    onChange={(page) => setKbDocPage(page)}
                  />
                </div>
              )}
            </section>
          </>
        )}
      </div>

      <Modal
        title={kbEditorMode === 'create' ? t('创建私有知识库') : t('编辑私有知识库')}
        open={kbEditorOpen}
        onCancel={closeKbEditor}
        maskClosable={false}
        width={520}
        className="jx-kbEditorModal"
        footer={(
          <div className="jx-kbEditorFooter">
            <Button onClick={closeKbEditor}>{t('取消')}</Button>
            <Button type="primary" loading={kbEditorLoading} onClick={() => void handleKbEditorSubmit()}>
              {kbEditorMode === 'create' ? t('创建') : t('保存')}
            </Button>
          </div>
        )}
      >
        <div className="jx-kbEditorBody">
          {kbEditorMode === 'create' && canCreatePrivateKb && canCreatePublicKb && (
            <div className="jx-kbEditorField">
              <div className="jx-kbEditorLabel">{t('可见性')}</div>
              <Radio.Group
                value={kbEditorVisibility}
                onChange={(e) => setKbEditorVisibility(e.target.value)}
                optionType="button"
                buttonStyle="solid"
                options={[
                  { label: t('私有（仅自己）'), value: 'private' },
                  { label: t('公有（全员可见）'), value: 'public' },
                ]}
              />
            </div>
          )}
          <div className="jx-kbEditorField">
            <div className="jx-kbEditorLabel">{t('知识库名称')}</div>
            <Input
              value={kbEditorName}
              onChange={(e) => setKbEditorName(e.target.value)}
              placeholder={t('请输入知识库名称')}
              maxLength={255}
            />
          </div>
          <div className="jx-kbEditorField">
            <div className="jx-kbEditorLabel">{t('知识库简介')}</div>
            <div className="jx-kbEditorTextareaWrap">
              <Input.TextArea
                value={kbEditorDesc}
                onChange={(e) => setKbEditorDesc(e.target.value)}
                placeholder={t('请输入知识库简介')}
                autoSize={{ minRows: 4, maxRows: 6 }}
                maxLength={500}
                className="jx-kbEditorTextarea"
              />
              <div className="jx-kbEditorTextareaMeta">
                <Button
                  size="small"
                  className="jx-kbEditorPolishBtn"
                  loading={kbEditorPolishing}
                  onClick={() => void handlePolishKbDescription()}
                  icon={<ThunderboltOutlined />}
                >
                  {t('AI润色')}
                </Button>
                <span className="jx-kbEditorCount">{kbEditorDesc.length} / 500</span>
              </div>
            </div>
          </div>
        </div>
      </Modal>

      <Modal
        title={
          <div className="jx-kbDocModalTitle">
            <span>{activeKbDoc?.title || activeKbDoc?.id || t('文档详情')}</span>
            {activeKbDoc && selectedId && selectedItem?.editable && (
              <Button size="small" icon={<ThunderboltOutlined />} onClick={() => openReindexModal(activeKbDoc.id, selectedId)}>
                {t('重新索引')}
              </Button>
            )}
          </div>
        }
        open={!!activeKbDoc}
        onCancel={() => { setActiveKbDoc(null); setDocDetailTab('content'); setDocChunks([]); }}
        footer={[<Button key="close" onClick={() => { setActiveKbDoc(null); setDocDetailTab('content'); setDocChunks([]); }}>{t('关闭')}</Button>]}
        width={920}
      >
        {activeKbDoc && (
          <div>
            {activeKbDoc.desc && <div className="jx-kbDocDesc">{activeKbDoc.desc}</div>}
            <div className="jx-kbDocTabs">
              <button className={`jx-kbDocTab${docDetailTab === 'content' ? ' active' : ''}`} onClick={() => setDocDetailTab('content')}>{t('内容预览')}</button>
              <button
                className={`jx-kbDocTab${docDetailTab === 'chunks' ? ' active' : ''}`}
                onClick={async () => {
                  setDocDetailTab('chunks');
                  if (docChunks.length === 0 && selectedId && activeKbDoc) {
                    setDocChunksLoading(true);
                    try {
                      const chunks = await getKBChunks(selectedId, activeKbDoc.id);
                      setDocChunks(chunks);
                      resetChunkChildren();
                    } catch {
                      message.error(t('加载分块失败'));
                    } finally {
                      setDocChunksLoading(false);
                    }
                  }
                }}
              >
                {t('分块列表')}{docChunks.length > 0 ? ` (${docChunks.length})` : ''}
              </button>
            </div>
            {docDetailTab === 'content' ? (
              <div className="jx-kbDocModalBody">
                {kbDocDetailLoadingId === activeKbDoc.id ? (
                  <div className="jx-kbDocLoading"><LoadingOutlined /> {t('正在加载文档正文…')}</div>
                ) : activeKbDoc.content ? (
                  <div className="jx-md jx-kbDocModalMarkdown" dangerouslySetInnerHTML={{ __html: mdToHtml(activeKbDoc.content) }} />
                ) : (
                  <Typography.Text type="secondary">{t('当前文档暂无正文内容。')}</Typography.Text>
                )}
              </div>
            ) : (
              <div style={{ maxHeight: '60vh', overflow: 'auto' }}>
                {docChunksLoading ? (
                  <div className="jx-kbDocLoading"><LoadingOutlined /> {t('正在加载分块列表…')}</div>
                ) : docChunks.length === 0 ? (
                  <Typography.Text type="secondary">{t('暂无分块数据。')}</Typography.Text>
                ) : (
                  <div className="jx-chunkList">
                    {docChunks.map((chunk) => (
                      <div key={chunk.chunk_id} className="jx-chunkCard">
                        <div className="jx-chunkHeader">
                          <div className="jx-chunkIndex"><span className="jx-chunkIndexNum">{chunk.chunk_index + 1}</span></div>
                          <div className="jx-chunkHeaderRight">
                            <span className="jx-chunkContentLen">{chunk.content.length} 字</span>
                            <Button
                              size="small"
                              type="link"
                              style={{ fontSize: 12, height: 28, paddingInline: 4 }}
                              loading={chunkChildrenLoading.has(chunk.chunk_id)}
                              onClick={() => toggleChunkChildren(chunk.chunk_id)}
                            >
                              {expandedChunkParents.has(chunk.chunk_id) ? t('收起子块') : t('查看子块')}
                            </Button>
                            {canEditChunks && (
                              <Button
                                size="small"
                                type={editingChunks.has(chunk.chunk_id) ? 'default' : 'link'}
                                icon={<EditOutlined />}
                                style={{ fontSize: 12, height: 28, paddingInline: 6 }}
                                onClick={() => toggleChunkEditing(chunk.chunk_id)}
                              >
                                {editingChunks.has(chunk.chunk_id) ? t('取消编辑') : t('编辑内容')}
                              </Button>
                            )}
                            {canEditChunks && (
                              <Button
                                size="small"
                                type="primary"
                                loading={chunkSaving === chunk.chunk_id}
                                style={{ borderRadius: 6, fontSize: 12, height: 28 }}
                                onClick={async () => {
                                  const isEditing = editingChunks.has(chunk.chunk_id);
                                  if (isEditing && !chunk.content.trim()) {
                                    message.error(t('分块内容不能为空'));
                                    return;
                                  }
                                  setChunkSaving(chunk.chunk_id);
                                  try {
                                    await updateKBChunk(selectedId!, chunk.chunk_id, {
                                      ...(isEditing ? { content: chunk.content } : {}),
                                      tags: chunk.tags,
                                      questions: chunk.questions,
                                    });
                                    message.success(t('分块已保存'));
                                    if (isEditing) {
                                      setEditingChunks((prev) => {
                                        const next = new Set(prev);
                                        next.delete(chunk.chunk_id);
                                        return next;
                                      });
                                    }
                                  } catch (err: any) {
                                    message.error(err.message || t('保存失败'));
                                  } finally {
                                    setChunkSaving(null);
                                  }
                                }}
                              >
                                {editingChunks.has(chunk.chunk_id) ? t('保存内容') : t('保存')}
                              </Button>
                            )}
                          </div>
                        </div>
                        {canEditChunks && editingChunks.has(chunk.chunk_id) ? (
                          <div className="jx-chunkContent" style={{ padding: 12 }}>
                            <Input.TextArea
                              value={chunk.content}
                              autoSize={{ minRows: 4, maxRows: 24 }}
                              onChange={(e) => {
                                const value = e.target.value;
                                setDocChunks((prev) => prev.map((item) => (item.chunk_id === chunk.chunk_id
                                  ? { ...item, content: value }
                                  : item)));
                              }}
                            />
                          </div>
                        ) : (
                          <div className="jx-chunkContent">{chunk.content}</div>
                        )}
                        {expandedChunkParents.has(chunk.chunk_id) && (
                          <div
                            style={{
                              marginTop: 8,
                              paddingLeft: 12,
                              borderLeft: '2px solid #D6E4FF',
                              display: 'flex',
                              flexDirection: 'column',
                              gap: 6,
                            }}
                          >
                            <span style={{ fontSize: 12, color: '#8c8c8c' }}>
                              {(chunkChildrenMap[chunk.chunk_id]?.length ?? 0) > 0
                                ? t('子块（{n}）— 向量检索的实际单位', { n: chunkChildrenMap[chunk.chunk_id]!.length })
                                : t('该父块无独立子块（扁平索引，父块即检索单位）')}
                            </span>
                            {(chunkChildrenMap[chunk.chunk_id] || []).map((ch) => (
                              <div key={ch.chunk_id} style={{ display: 'flex', gap: 8 }}>
                                <Tag color="geekblue" style={{ height: 'fit-content', margin: 0 }}>{ch.chunk_index + 1}</Tag>
                                <div
                                  style={{
                                    flex: 1,
                                    fontSize: 12,
                                    color: '#595959',
                                    whiteSpace: 'pre-wrap',
                                    wordBreak: 'break-word',
                                    background: '#FAFBFC',
                                    border: '1px solid #EEF1F4',
                                    borderRadius: 6,
                                    padding: '6px 8px',
                                    lineHeight: 1.6,
                                  }}
                                >
                                  {ch.content}
                                </div>
                              </div>
                            ))}
                          </div>
                        )}
                        {(chunk.tags.length > 0 || chunk.questions.length > 0) && (
                          <div className="jx-chunkMeta">
                            {chunk.tags.length > 0 && (
                              <div className="jx-chunkSection">
                                <div className="jx-chunkSectionLabel"><span className="jx-chunkSectionIcon">🏷</span>{t('标签')}</div>
                                <div className="jx-chunkTagsWrap">
                                  {chunk.tags.map((tag, ti) => (
                                    <Tag
                                      key={ti}
                                      closable={canEditChunks}
                                      className="jx-chunkTag"
                                      onClose={() => setDocChunks((prev) => prev.map((item) => (item.chunk_id === chunk.chunk_id
                                        ? { ...item, tags: item.tags.filter((_, index) => index !== ti) }
                                        : item)))}
                                    >
                                      {tag}
                                    </Tag>
                                  ))}
                                  {canEditChunks && (
                                    <Input
                                      size="small"
                                      placeholder={t('+ 标签')}
                                      className="jx-chunkAddInput"
                                      onPressEnter={(e) => {
                                        const value = (e.target as HTMLInputElement).value.trim();
                                        if (!value) return;
                                        setDocChunks((prev) => prev.map((item) => (item.chunk_id === chunk.chunk_id
                                          ? { ...item, tags: [...item.tags, value] }
                                          : item)));
                                        (e.target as HTMLInputElement).value = '';
                                      }}
                                    />
                                  )}
                                </div>
                              </div>
                            )}
                            {chunk.questions.length > 0 && (
                              <div className="jx-chunkSection">
                                <div className="jx-chunkSectionLabel"><span className="jx-chunkSectionIcon">💬</span>{t('关联问题')}</div>
                                <div className="jx-chunkQuestions">
                                  {chunk.questions.map((question, qi) => (
                                    <div key={qi} className="jx-chunkQuestion">
                                      <span className="jx-chunkQuestionText">{question}</span>
                                      {canEditChunks && (
                                        <Button
                                          type="text"
                                          size="small"
                                          className="jx-chunkQuestionDel"
                                          icon={<CloseOutlined style={{ fontSize: 10 }} />}
                                          onClick={() => setDocChunks((prev) => prev.map((item) => (item.chunk_id === chunk.chunk_id
                                            ? { ...item, questions: item.questions.filter((_, index) => index !== qi) }
                                            : item)))}
                                        />
                                      )}
                                    </div>
                                  ))}
                                  {canEditChunks && (
                                    <Input
                                      size="small"
                                      placeholder={t('+ 问题')}
                                      className="jx-chunkAddInput"
                                      style={{ marginTop: 4 }}
                                      onPressEnter={(e) => {
                                        const value = (e.target as HTMLInputElement).value.trim();
                                        if (!value) return;
                                        setDocChunks((prev) => prev.map((item) => (item.chunk_id === chunk.chunk_id
                                          ? { ...item, questions: [...item.questions, value] }
                                          : item)));
                                        (e.target as HTMLInputElement).value = '';
                                      }}
                                    />
                                  )}
                                </div>
                              </div>
                            )}
                          </div>
                        )}
                        {chunk.tags.length === 0 && chunk.questions.length === 0 && canEditChunks && (
                          <div className="jx-chunkMeta">
                            <div className="jx-chunkSection">
                              <div className="jx-chunkTagsWrap">
                                <Input
                                  size="small"
                                  placeholder={t('+ 标签')}
                                  className="jx-chunkAddInput"
                                  onPressEnter={(e) => {
                                    const value = (e.target as HTMLInputElement).value.trim();
                                    if (!value) return;
                                    setDocChunks((prev) => prev.map((item) => (item.chunk_id === chunk.chunk_id
                                      ? { ...item, tags: [...item.tags, value] }
                                      : item)));
                                    (e.target as HTMLInputElement).value = '';
                                  }}
                                />
                                <Input
                                  size="small"
                                  placeholder={t('+ 问题')}
                                  className="jx-chunkAddInput"
                                  onPressEnter={(e) => {
                                    const value = (e.target as HTMLInputElement).value.trim();
                                    if (!value) return;
                                    setDocChunks((prev) => prev.map((item) => (item.chunk_id === chunk.chunk_id
                                      ? { ...item, questions: [...item.questions, value] }
                                      : item)));
                                    (e.target as HTMLInputElement).value = '';
                                  }}
                                />
                              </div>
                            </div>
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </Modal>

      <Modal
        title={t('上传文档到「{name}」', { name: selectedItem?.name || '' })}
        open={uploadDocModalOpen}
        onCancel={() => closeUploadDocModal()}
        maskClosable={false}
        width={uploadStep === 'preview' ? 720 : 520}
        className="jx-kbUploadModal"
        footer={uploadStep === 'config' ? (
          <div className="jx-kbUploadModalFooter">
            <Button onClick={() => closeUploadDocModal()}>{t('取消')}</Button>
            <Button loading={chunkPreviewLoading} disabled={uploadDocFileList.length === 0} onClick={() => void handleUpload(true)}>{t('预览分块')}</Button>
            <Button type="primary" loading={uploadDocLoading} disabled={uploadDocFileList.length === 0} onClick={() => void handleUpload(false)}>{t('直接上传')}</Button>
          </div>
        ) : (
          <div className="jx-kbUploadModalFooter">
            <Button onClick={() => { setUploadStep('config'); setChunkPreviewData(null); setExpandedChunkIndex(null); }}>{t('返回修改')}</Button>
            <Button type="primary" loading={uploadDocLoading} onClick={() => void handleUpload(false)}>{t('确认上传')}</Button>
          </div>
        )}
      >
        {/* config↔preview cross-slide; initial={false} avoids stacking with antd Modal's built-in animation */}
        <AnimatePresence mode="wait" initial={false}>
        {uploadStep === 'config' ? (
          <motion.div
            key="config"
            className="jx-kbUploadModalBody"
            initial={{ opacity: 0, x: -12 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: -12 }}
            transition={{ duration: 0.18, ease: EASE.standard }}
          >
            <Upload.Dragger
              className="jx-kbUploadDragger"
              multiple
              accept=".pdf,.docx,.doc,.txt,.md,.csv,.json,.xlsx,.xls"
              beforeUpload={(file) => { setUploadDocFileList((prev) => [...prev, file]); return false; }}
              onRemove={(file) => setUploadDocFileList((prev) => prev.filter((item) => item.name !== file.name || item.size !== file.size))}
              fileList={uploadDocFileList.map((file) => ({ uid: `${file.name}-${file.size}`, name: file.name, size: file.size, status: 'done' as const }))}
            >
              <p className="ant-upload-drag-icon"><InboxOutlined /></p>
              <p className="ant-upload-text">{t('点击或拖拽文件到此区域')}</p>
              <p className="ant-upload-hint">{t('支持 PDF、Word、Excel、TXT、Markdown、CSV、JSON，单文件最大 100MB')}</p>
            </Upload.Dragger>
            <div className="jx-kbUploadSection">
              <div className="jx-kbUploadFieldLabel">{t('分块方法')}</div>
              <Select
                value={uploadChunkMethod}
                onChange={setUploadChunkMethod}
                className="jx-kbUploadSelect"
                popupClassName="jx-kbUploadSelectDropdown"
                options={UPLOAD_CHUNK_METHOD_OPTIONS.map((option) => ({
                  value: option.value,
                  label: option.label,
                  desc: option.desc,
                  recommended: option.recommended,
                }))}
                optionRender={(option) => {
                  const data = option.data as {
                    label: string;
                    desc?: string;
                    recommended?: boolean;
                  };
                  return (
                    <div className="jx-kbUploadOption">
                      <div className="jx-kbUploadOptionTop">
                        <span className="jx-kbUploadOptionTitle">{data.label}</span>
                        {data.recommended && (
                          <span className="jx-kbUploadOptionBadge">
                            <StarFilled />
                            <span>{t('推荐')}</span>
                          </span>
                        )}
                      </div>
                      {data.desc && <div className="jx-kbUploadOptionDesc">{data.desc}</div>}
                    </div>
                  );
                }}
              />
            </div>
            <div className="jx-kbUploadToggleRow">
              <div className="jx-kbUploadToggleCopy">
                <Typography.Text className="jx-kbUploadToggleTitle">{t('启用父子分块')}</Typography.Text>
                <Typography.Text type="secondary" className="jx-kbUploadToggleDesc">
                  {uploadParentChildIndexing ? t('父块存储完整上下文，子块用于向量检索') : t('关闭后仅按块索引，不拆分子块')}
                </Typography.Text>
              </div>
              <Switch size="small" checked={uploadParentChildIndexing} onChange={setUploadParentChildIndexing} />
            </div>
            <Collapse
              className="jx-kbUploadCollapse"
              ghost
              items={[{
                key: 'advanced',
                label: <Typography.Text type="secondary" className="jx-kbUploadCollapseLabel">{t('高级索引设置')}</Typography.Text>,
                children: (
                  <div className="jx-kbUploadAdvanced">
                    <div className="jx-kbUploadAdvancedRow">
                      <div className="jx-kbUploadAdvancedCol">
                        <div className="jx-kbUploadFieldLabel">{uploadParentChildIndexing ? t('父块大小（token）') : t('块大小（token）')}</div>
                        <InputNumber min={256} max={4096} step={128} value={uploadParentChunkSize} onChange={(v) => setUploadParentChunkSize(v ?? 1024)} style={{ width: '100%' }} />
                      </div>
                      {uploadParentChildIndexing && (
                        <div className="jx-kbUploadAdvancedCol">
                          <div className="jx-kbUploadFieldLabel">{t('子块大小（token）')}</div>
                          <InputNumber min={64} max={512} step={32} value={uploadChildChunkSize} onChange={(v) => setUploadChildChunkSize(v ?? 128)} style={{ width: '100%' }} />
                        </div>
                      )}
                      {uploadParentChildIndexing && (
                        <div className="jx-kbUploadAdvancedCol">
                          <div className="jx-kbUploadFieldLabel">{t('重叠 token')}</div>
                          <InputNumber min={0} max={100} value={uploadOverlapTokens} onChange={(v) => setUploadOverlapTokens(v ?? 20)} style={{ width: '100%' }} />
                        </div>
                      )}
                    </div>
                    <div className="jx-kbUploadAdvancedRow">
                      <div className="jx-kbUploadAdvancedCol">
                        <div className="jx-kbUploadFieldLabel">{t('自动关键词数（0=关闭）')}</div>
                        <InputNumber min={0} max={10} value={uploadAutoKeywordsCount} onChange={(v) => setUploadAutoKeywordsCount(v ?? 0)} style={{ width: '100%' }} />
                      </div>
                      <div className="jx-kbUploadAdvancedCol">
                        <div className="jx-kbUploadFieldLabel">{t('自动问题数（0=关闭）')}</div>
                        <InputNumber min={0} max={10} value={uploadAutoQuestionsCount} onChange={(v) => setUploadAutoQuestionsCount(v ?? 0)} style={{ width: '100%' }} />
                      </div>
                    </div>
                    <div className="jx-kbUploadAdvancedRow">
                      <div className="jx-kbUploadAdvancedCol" style={{ flex: '1 1 100%' }}>
                        <div className="jx-kbUploadFieldLabel">
                          {uploadParentChildIndexing ? t('父分块分隔符（每行一个，留空用默认）') : t('自定义分隔符（每行一个，留空用默认）')}
                        </div>
                        <Input.TextArea
                          value={uploadSeparators}
                          onChange={(e) => setUploadSeparators(e.target.value)}
                          autoSize={{ minRows: 2, maxRows: 6 }}
                          placeholder={'\\n\\n\n。\n；'}
                          spellCheck={false}
                        />
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          {t('「递归分块」以此为分块依据（相邻小片段会合并到父块大小）；语义分块仅超长时用它兜底。可用 \\n \\t 表示换行/制表符')}
                        </Typography.Text>
                      </div>
                    </div>
                    {uploadParentChildIndexing && (
                      <div className="jx-kbUploadAdvancedRow">
                        <div className="jx-kbUploadAdvancedCol" style={{ flex: '1 1 100%' }}>
                          <div className="jx-kbUploadFieldLabel">{t('子分块分隔符（每行一个，留空用默认）')}</div>
                          <Input.TextArea
                            value={uploadChildSeparators}
                            onChange={(e) => setUploadChildSeparators(e.target.value)}
                            autoSize={{ minRows: 2, maxRows: 6 }}
                            placeholder={'\\n\n。'}
                            spellCheck={false}
                          />
                          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                            {t('父块拆成子块时按这些分隔符切，再按子块大小打包；为空走定长滑窗。可用 \\n \\t 表示换行/制表符')}
                          </Typography.Text>
                        </div>
                      </div>
                    )}
                  </div>
                ),
              }]}
            />
          </motion.div>
        ) : (
          <motion.div
            key="preview"
            className="jx-kbChunkPreview"
            initial={{ opacity: 0, x: 12 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 12 }}
            transition={{ duration: 0.18, ease: EASE.standard }}
          >
            <div className="jx-kbChunkPreviewSummary">
              {t('共预览 {n} 个分块', { n: chunkPreviewData?.total_chunks || 0 })}
              {uploadParentChildIndexing && t(' / {n} 个子块', { n: chunkPreviewData?.total_children || 0 })}
            </div>
            <div className="jx-kbChunkPreviewList">
              {(chunkPreviewData?.chunks || []).map((chunk) => (
                <div key={chunk.index} className="jx-kbChunkPreviewCard">
                  <button
                    className="jx-kbChunkPreviewHeader"
                    onClick={() => setExpandedChunkIndex(expandedChunkIndex === chunk.index ? null : chunk.index)}
                  >
                    <span>{t('分块 {n}', { n: chunk.index + 1 })}</span>
                    <span>{chunk.token_count} tokens / {chunk.children_count} 子块</span>
                  </button>
                  <div className="jx-kbChunkPreviewBody">{chunk.content}</div>
                  {expandedChunkIndex === chunk.index && chunk.children_preview.length > 0 && (
                    <div className="jx-kbChunkPreviewChildren">
                      {chunk.children_preview.map((child) => (
                        <div key={child.index} className="jx-kbChunkPreviewChild">
                          <div className="jx-kbChunkPreviewChildIndex">{t('子块 {n}', { n: child.index + 1 })}</div>
                          <div>{child.content}</div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </motion.div>
        )}
        </AnimatePresence>
      </Modal>
    </>
  );
}
