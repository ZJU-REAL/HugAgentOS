import { ApartmentOutlined, ArrowRightOutlined } from '@ant-design/icons';
import type { ReactNode } from 'react';

import { t } from '../../../i18n';
import { useCanvasStore, useChatStore } from '../../../stores';
import { parseIndustryChainOutput } from '../../../utils/industryChain';
import { preview } from './utils';

interface DetailModal {
  title: string;
  body: ReactNode;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function renderValue(value: unknown): ReactNode {
  if (value == null) return <span className="jx-tr-chainNull">—</span>;
  if (typeof value === 'number') {
    return <span className="jx-tr-chainNum">{value.toLocaleString()}</span>;
  }
  if (typeof value === 'string' || typeof value === 'boolean') return <span>{String(value)}</span>;
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="jx-tr-chainNull">{t('暂无数据')}</span>;
    const firstRecord = asRecord(value[0]);
    if (firstRecord) {
      const keys = Object.keys(firstRecord);
      return (
        <div className="jx-tr-chainMiniTable">
          {value.map((row, rowIndex) => {
            const rowRecord = asRecord(row) ?? {};
            return (
              <div key={rowIndex} className="jx-tr-chainMiniRow">
                {keys.map((key) => (
                  <span key={key} className="jx-tr-chainMiniCell">
                    <span className="jx-tr-chainMiniKey">{key}</span>
                    <span className="jx-tr-chainMiniVal">{rowRecord[key] == null ? '—' : String(rowRecord[key])}</span>
                  </span>
                ))}
              </div>
            );
          })}
        </div>
      );
    }
    return <span>{value.map(String).join('、')}</span>;
  }

  const record = asRecord(value);
  if (!record) return <span>{String(value)}</span>;
  return (
    <div className="jx-tr-chainKV">
      {Object.entries(record).map(([key, nestedValue]) => (
        <div key={key} className="jx-tr-chainKVRow">
          <span className="jx-tr-chainKVKey">{key}</span>
          <div className="jx-tr-chainKVVal">{renderValue(nestedValue)}</div>
        </div>
      ))}
    </div>
  );
}

function sectionPreview(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number') return String(value);
  if (Array.isArray(value)) return t('{n} 条数据', { n: value.length });
  const record = asRecord(value);
  if (!record) return '';
  if (record['名称'] != null) return String(record['名称']);
  if (record['描述'] != null) return preview(String(record['描述']));
  return Object.keys(record).slice(0, 3).join('、');
}

export function renderIndustryChain(
  output: unknown,
  setDetailModal: (modal: DetailModal | null) => void,
): ReactNode {
  const model = parseIndustryChainOutput(output);
  if (model.error && !model.tree) return <div className="jx-tr-chainText">{model.error}</div>;

  const openMindMap = () => {
    useCanvasStore.getState().openIndustryChain({
      chatId: useChatStore.getState().currentChatId,
      toolId: `history:${model.title}`,
      chainName: model.title,
      status: model.tree ? 'success' : 'error',
      output,
      ...(!model.tree ? { error: t('未返回可展示的产业链结构') } : {}),
    });
  };

  return (
    <div className="jx-tr-chainResult">
      <button className="jx-tr-chainMapCard" onClick={openMindMap} type="button">
        <span className="jx-tr-chainMapIcon"><ApartmentOutlined /></span>
        <span className="jx-tr-chainMapText">
          <strong>{t('产业链图谱')}</strong>
          <small>{model.tree
            ? t('已生成 {n} 个节点，点击在 Canvas 中查看', { n: model.nodeCount })
            : t('暂无可视化图谱数据')}</small>
        </span>
        <ArrowRightOutlined className="jx-tr-chainMapArrow" />
      </button>

      <div className="jx-tr-chainSections">
        {Object.entries(model.sections)
          .filter(([sectionKey]) => sectionKey !== '产业链图谱')
          .map(([sectionKey, sectionValue]) => {
            const summary = sectionPreview(sectionValue);
            const openDetail = () => setDetailModal({
              title: sectionKey,
              body: <div className="jx-tr-chainDetailWrap">{renderValue(sectionValue)}</div>,
            });
            return (
              <div
                key={sectionKey}
                className="jx-tr-chainSection jx-tr-chainSection--clickable"
                onClick={openDetail}
                title={t('点击查看详情')}
              >
                <div className="jx-tr-chainSectionKey">{sectionKey}</div>
                {summary && <div className="jx-tr-chainSectionVal">{summary}</div>}
              </div>
            );
          })}
      </div>
    </div>
  );
}
