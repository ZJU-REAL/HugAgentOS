import { useState } from 'react';
import { Collapse, Input, InputNumber, Modal, Select, Switch, Typography, message } from 'antd';
import { useKbStore } from '../../stores';
import { reindexKBDocument } from '../../api';
import type { IndexingConfig } from '../../api';
import { parseSeparators } from '../../utils/separators';
import { t } from '../../i18n';

const CHUNK_METHOD_OPTIONS = [
  { value: 'structured', label: t('结构感知（按标题和段落）') },
  { value: 'recursive', label: t('递归分块（多级分隔符）') },
  { value: 'embedding_semantic', label: t('语义分块（基于嵌入相似度）⭐ 推荐') },
  { value: 'laws', label: t('法律文书') },
  { value: 'qa', label: t('问答对') },
];

const labelStyle = { marginBottom: 4, fontSize: 12, color: '#808080' } as const;

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
      width={560}
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
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <Typography.Text type="secondary" style={{ fontSize: 13 }}>
          {t('将删除现有分块并使用下方参数重新解析索引。')}
        </Typography.Text>
        <div>
          <div style={labelStyle}>{t('分块方法')}</div>
          <Select
            value={reindexChunkMethod}
            onChange={setReindexChunkMethod}
            style={{ width: '100%' }}
            options={CHUNK_METHOD_OPTIONS}
          />
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Switch checked={parentChildIndexing} onChange={setParentChildIndexing} />
          <span style={{ fontSize: 13 }}>{t('启用父子分块')}</span>
        </div>
        <Collapse
          ghost
          defaultActiveKey={['adv']}
          items={[{
            key: 'adv',
            label: <Typography.Text type="secondary">{t('高级索引设置')}</Typography.Text>,
            children: (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
                  <div>
                    <div style={labelStyle}>{parentChildIndexing ? t('父块大小（token）') : t('块大小（token）')}</div>
                    <InputNumber min={256} max={4096} step={128} value={parentChunkSize} onChange={(v) => setParentChunkSize(v ?? 1024)} />
                  </div>
                  {parentChildIndexing && (
                    <div>
                      <div style={labelStyle}>{t('子块大小（token）')}</div>
                      <InputNumber min={64} max={512} step={32} value={childChunkSize} onChange={(v) => setChildChunkSize(v ?? 128)} />
                    </div>
                  )}
                  {parentChildIndexing && (
                    <div>
                      <div style={labelStyle}>{t('重叠 token')}</div>
                      <InputNumber min={0} max={100} value={overlapTokens} onChange={(v) => setOverlapTokens(v ?? 20)} />
                    </div>
                  )}
                </div>
                <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
                  <div>
                    <div style={labelStyle}>{t('自动关键词数（0=关闭）')}</div>
                    <InputNumber min={0} max={10} value={autoKeywordsCount} onChange={(v) => setAutoKeywordsCount(v ?? 0)} />
                  </div>
                  <div>
                    <div style={labelStyle}>{t('自动问题数（0=关闭）')}</div>
                    <InputNumber min={0} max={10} value={autoQuestionsCount} onChange={(v) => setAutoQuestionsCount(v ?? 0)} />
                  </div>
                </div>
                <div>
                  <div style={labelStyle}>
                    {parentChildIndexing ? t('父分块分隔符（每行一个，留空用默认）') : t('自定义分隔符（每行一个，留空用默认）')}
                  </div>
                  <Input.TextArea
                    value={separators}
                    onChange={(e) => setSeparators(e.target.value)}
                    autoSize={{ minRows: 2, maxRows: 6 }}
                    placeholder={'\\n\\n\n。\n；'}
                    spellCheck={false}
                  />
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {t('「递归分块」以此为分块依据（相邻小片段会合并到父块大小）；语义分块仅超长时用它兜底。可用 \\n \\t 表示换行/制表符')}
                  </Typography.Text>
                </div>
                {parentChildIndexing && (
                  <div>
                    <div style={labelStyle}>{t('子分块分隔符（每行一个，留空用默认）')}</div>
                    <Input.TextArea
                      value={childSeparators}
                      onChange={(e) => setChildSeparators(e.target.value)}
                      autoSize={{ minRows: 2, maxRows: 6 }}
                      placeholder={'\\n\n。'}
                      spellCheck={false}
                    />
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {t('父块拆成子块时按这些分隔符切，再按子块大小打包；为空走定长滑窗。可用 \\n \\t 表示换行/制表符')}
                    </Typography.Text>
                  </div>
                )}
              </div>
            ),
          }]}
        />
      </div>
    </Modal>
  );
}
