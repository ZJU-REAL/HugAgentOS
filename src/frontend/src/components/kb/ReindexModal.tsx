import { useState } from 'react';
import { Form, Modal, Typography, message } from 'antd';
import { useKbStore } from '../../stores';
import { reindexKBDocument } from '../../api';
import type { IndexingConfig } from '../../api';
import { parseSeparators } from '../../utils/separators';
import { t } from '../../i18n';
import { COMPACT_MODAL_PROPS } from '../common/modalLayout';
import { IndexingConfigFields } from './IndexingConfigFields';

export default function ReindexModal() {
  const {
    reindexModalOpen,
    reindexChunkMethod,
    reindexDocId,
    reindexKbId,
    reindexLoading,
    closeReindexModal,
    setReindexChunkMethod,
    setReindexLoading,
    activeKbDoc,
    setActiveKbDoc,
    setKbDocumentsMap,
  } = useKbStore();

  // Index parameters adjustable on rebuild (same as upload)
  const [parentChildIndexing, setParentChildIndexing] = useState(true);
  const [parentChunkSize, setParentChunkSize] = useState(1024);
  const [childChunkSize, setChildChunkSize] = useState(128);
  const [overlapTokens, setOverlapTokens] = useState(20);
  const [autoKeywordsCount, setAutoKeywordsCount] = useState(0);
  const [autoQuestionsCount, setAutoQuestionsCount] = useState(0);
  const [separators, setSeparators] = useState('');
  const [childSeparators, setChildSeparators] = useState('');

  return (
    <Modal
      title={t('重新索引文档')}
      open={reindexModalOpen}
      onCancel={closeReindexModal}
      confirmLoading={reindexLoading}
      okText={t('开始索引')}
      cancelText={t('取消')}
      width={720}
      {...COMPACT_MODAL_PROPS}
      onOk={async () => {
        if (!reindexKbId || !reindexDocId) return;
        setReindexLoading(true);
        try {
          const sep = parseSeparators(separators);
          const csep = parseSeparators(childSeparators);
          const idxCfg: IndexingConfig = {
            parent_chunk_size: parentChunkSize,
            child_chunk_size: childChunkSize,
            overlap_tokens: overlapTokens,
            parent_child_indexing: parentChildIndexing,
            auto_keywords_count: autoKeywordsCount,
            auto_questions_count: autoQuestionsCount,
            ...(sep.length ? { separators: sep } : {}),
            ...(csep.length && parentChildIndexing ? { child_separators: csep } : {}),
          };
          await reindexKBDocument(reindexKbId, reindexDocId, idxCfg, reindexChunkMethod);
          message.success(t('重新索引已启动'));
          if (activeKbDoc?.id === reindexDocId) {
            setActiveKbDoc({ ...activeKbDoc, indexing_status: 'processing' });
          }
          setKbDocumentsMap(prev => {
            const docs = prev[reindexKbId!];
            if (!Array.isArray(docs)) return prev;
            return { ...prev, [reindexKbId!]: docs.map(d => d.id === reindexDocId ? { ...d, indexing_status: 'processing' } : d) };
          });
          closeReindexModal();
        } catch (err: any) {
          message.error(err.message || t('重新索引失败'));
        } finally {
          setReindexLoading(false);
        }
      }}
    >
      <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 12, fontSize: 13 }}>
        {t('将删除现有分块并使用下方参数重新解析索引。')}
      </Typography.Text>
      <Form layout="vertical">
        <IndexingConfigFields s={{
          chunkMethod: reindexChunkMethod, setChunkMethod: setReindexChunkMethod,
          parentChildIndexing, setParentChildIndexing,
          parentChunkSize, setParentChunkSize, childChunkSize, setChildChunkSize,
          overlapTokens, setOverlapTokens, autoKeywordsCount, setAutoKeywordsCount,
          autoQuestionsCount, setAutoQuestionsCount, separators, setSeparators,
          childSeparators, setChildSeparators,
        }} />
      </Form>
    </Modal>
  );
}
