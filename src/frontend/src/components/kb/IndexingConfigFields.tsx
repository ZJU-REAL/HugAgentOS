// Indexing parameters for upload / reindex (admin public KB and the chat-app reindex dialog).
import { Form, Input, InputNumber, Select, Switch, Tooltip } from 'antd';
import { QuestionCircleOutlined } from '@ant-design/icons';
import { t } from '../../i18n';
import { AdvancedFields, FormGrid } from '../common/FormLayout';
import { CHUNK_METHOD_OPTIONS } from './chunkMethods';

export interface IndexingConfigValues {
  chunkMethod: string;
  setChunkMethod: (v: string) => void;
  parentChildIndexing: boolean;
  setParentChildIndexing: (v: boolean) => void;
  parentChunkSize: number;
  setParentChunkSize: (v: number) => void;
  childChunkSize: number;
  setChildChunkSize: (v: number) => void;
  overlapTokens: number;
  setOverlapTokens: (v: number) => void;
  autoKeywordsCount: number;
  setAutoKeywordsCount: (v: number) => void;
  autoQuestionsCount: number;
  setAutoQuestionsCount: (v: number) => void;
  separators: string;
  setSeparators: (v: string) => void;
  childSeparators: string;
  setChildSeparators: (v: string) => void;
}

// Indexing-parameter form: method + parent-child toggle on one row; advanced numbers in a
// three-column grid and the two separator boxes side by side, collapsed by default.
export function IndexingConfigFields({ s }: { s: IndexingConfigValues }) {
  const pc = s.parentChildIndexing;
  return (
    <>
      <FormGrid>
        <Form.Item label={t('分块方法')}>
          <Select value={s.chunkMethod} onChange={s.setChunkMethod} options={CHUNK_METHOD_OPTIONS} />
        </Form.Item>
        <div className="jx-formGrid-alignField">
          <div className="jx-formToggle">
            <span className="jx-formToggle-label">
              {t('启用父子分块')}
              <Tooltip title={t('父块存储完整上下文，子块用于向量检索；关闭后仅按块索引，不拆分子块')}>
                <QuestionCircleOutlined className="jx-formToggle-help" aria-label={t('说明')} />
              </Tooltip>
            </span>
            <Switch size="small" checked={pc} onChange={s.setParentChildIndexing} aria-label={t('启用父子分块')} />
          </div>
        </div>
      </FormGrid>
      <AdvancedFields title={t('高级索引设置')} hint={t('块大小、自动关键词、分隔符')}>
        <FormGrid cols={3}>
          <Form.Item label={pc ? t('父块大小（token）') : t('块大小（token）')}>
            <InputNumber min={256} max={4096} step={128} style={{ width: '100%' }} value={s.parentChunkSize} onChange={(v) => s.setParentChunkSize(v ?? 1024)} />
          </Form.Item>
          {pc && (
            <Form.Item label={t('子块大小（token）')}>
              <InputNumber min={64} max={512} step={32} style={{ width: '100%' }} value={s.childChunkSize} onChange={(v) => s.setChildChunkSize(v ?? 128)} />
            </Form.Item>
          )}
          {pc && (
            <Form.Item label={t('重叠 token')}>
              <InputNumber min={0} max={100} style={{ width: '100%' }} value={s.overlapTokens} onChange={(v) => s.setOverlapTokens(v ?? 20)} />
            </Form.Item>
          )}
          <Form.Item label={t('自动关键词数')} tooltip={t('0 = 关闭')}>
            <InputNumber min={0} max={10} style={{ width: '100%' }} value={s.autoKeywordsCount} onChange={(v) => s.setAutoKeywordsCount(v ?? 0)} />
          </Form.Item>
          <Form.Item label={t('自动问题数')} tooltip={t('0 = 关闭')}>
            <InputNumber min={0} max={10} style={{ width: '100%' }} value={s.autoQuestionsCount} onChange={(v) => s.setAutoQuestionsCount(v ?? 0)} />
          </Form.Item>
        </FormGrid>
        <FormGrid>
          <Form.Item
            label={pc ? t('父分块分隔符') : t('自定义分隔符')}
            tooltip={t('每行一个，留空用默认。「递归分块」以此为分块依据（相邻小片段会合并到父块大小）；语义分块仅超长时用它兜底。可用 \\n \\t 表示换行/制表符')}
            className={pc ? undefined : 'jx-formGrid-full'}
          >
            <Input.TextArea
              value={s.separators}
              onChange={(e) => s.setSeparators(e.target.value)}
              autoSize={{ minRows: 2, maxRows: 4 }}
              placeholder={'\\n\\n\n。\n；'}
              spellCheck={false}
            />
          </Form.Item>
          {pc && (
            <Form.Item
              label={t('子分块分隔符')}
              tooltip={t('每行一个，留空用默认。父块拆成子块时按这些分隔符切，再按子块大小打包；为空走定长滑窗。可用 \\n \\t 表示换行/制表符')}
            >
              <Input.TextArea
                value={s.childSeparators}
                onChange={(e) => s.setChildSeparators(e.target.value)}
                autoSize={{ minRows: 2, maxRows: 4 }}
                placeholder={'\\n\n。'}
                spellCheck={false}
              />
            </Form.Item>
          )}
        </FormGrid>
      </AdvancedFields>
    </>
  );
}
