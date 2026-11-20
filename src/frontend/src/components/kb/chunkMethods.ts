import { t } from '../../i18n';

/** Chunking methods offered when (re)indexing a knowledge-base document — user ReindexModal and the admin KB manager. */
export const CHUNK_METHOD_OPTIONS = [
  { value: 'structured', label: t('结构感知（按标题和段落）') },
  { value: 'recursive', label: t('递归分块（多级分隔符）') },
  { value: 'embedding_semantic', label: t('语义分块（基于嵌入相似度）⭐ 推荐') },
  { value: 'laws', label: t('法律文书') },
  { value: 'qa', label: t('问答对') },
];
