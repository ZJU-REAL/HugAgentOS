import React from 'react';
import { authFetch } from '../../../api';
import { PREVIEW_LEN, preview } from './utils';
import { t } from '../../../i18n';

const effectiveApiUrl = (import.meta.env.VITE_API_BASE_URL as string || '').trim() || '/api';

type SetDetailModal = (modal: { title: string; body: React.ReactNode } | null) => void;

export function renderRetrieveDatasetContent(out: unknown, setDetailModal: SetDetailModal): React.ReactNode {
  const empty = (msg: string) => <div className="jx-tr-empty">{msg}</div>;
  const data = (typeof out === 'object' && out !== null ? out : {}) as any;
  const items: any[] = Array.isArray(data?.items) ? data.items : [];
  if (items.length === 0) return empty(t('未检索到相关内容'));
  return (
    <div className="jx-tr-kbList">
      {items.map((item: any, idx: number) => {
        const docName = item['文件名称'] || item.title || item.document_name || t('未知文档');
        const content = String(item['文件内容'] || item.content || '');
        const datasetId: string = item['dataset_id'] || '';
        const documentId: string = item['document_id'] || '';
        const canFetchFull = !!(datasetId && documentId);
        const openDetail = async () => {
          if (canFetchFull) {
            setDetailModal({
              title: docName,
              body: <div className="jx-tr-detailBody" style={{ color: '#B3B3B3' }}>{t('正在加载全文…')}</div>,
            });
            try {
              const resp = await authFetch(`${effectiveApiUrl}/v1/catalog/kb/${datasetId}/documents/${documentId}`);
              const json = await resp.json();
              const detail = json?.data ?? json;
              const fullContent: string = detail?.content || content || t('暂无内容');
              const fullTitle: string = detail?.title || docName;
              setDetailModal({ title: fullTitle, body: <div className="jx-tr-detailBody">{fullContent}</div> });
            } catch {
              setDetailModal({ title: docName, body: <div className="jx-tr-detailBody">{content || t('暂无内容')}</div> });
            }
          } else {
            setDetailModal({ title: docName, body: <div className="jx-tr-detailBody">{content || t('暂无内容')}</div> });
          }
        };
        return (
          <div key={idx} className="jx-tr-kbItem jx-tr-kbItem--clickable" onClick={openDetail} title={t('点击查看全文')}>
            <div className="jx-tr-kbDocName"><span className="jx-tr-kbIdx">{idx + 1}</span>{docName}</div>
            {content && (
              <div className="jx-tr-kbPreview">
                <div className="jx-tr-kbContent">{preview(content)}</div>
                {content.length > PREVIEW_LEN && <span className="jx-tr-kbMore">{t('查看全文 →')}</span>}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function renderRetrieveLocalKB(out: unknown, setDetailModal: SetDetailModal): React.ReactNode {
  const empty = (msg: string) => <div className="jx-tr-empty">{msg}</div>;
  const data = (typeof out === 'object' && out !== null ? out : {}) as any;
  const items: any[] = Array.isArray(data?.items) ? data.items : Array.isArray(data) ? data : [];
  if (items.length === 0) return empty(t('未检索到相关内容'));
  return (
    <div className="jx-tr-kbList">
      {items.map((item: any, idx: number) => {
        const docName = item.title || t('未知文档');
        const content = String(item.content || '');
        const score = item.score != null ? t('相关度 {n}%', { n: (item.score * 100).toFixed(1) }) : '';
        const openDetail = () => {
          setDetailModal({ title: docName, body: <div className="jx-tr-detailBody">{content || t('暂无内容')}</div> });
        };
        return (
          <div key={idx} className="jx-tr-kbItem jx-tr-kbItem--clickable" onClick={openDetail} title={t('点击查看全文')}>
            <div className="jx-tr-kbDocName"><span className="jx-tr-kbIdx">{idx + 1}</span>{docName}</div>
            {content && (
              <div className="jx-tr-kbPreview">
                <div className="jx-tr-kbContent">{preview(content)}</div>
                {content.length > PREVIEW_LEN && <span className="jx-tr-kbMore">{t('查看全文 →')}</span>}
              </div>
            )}
            {score && <div className="jx-tr-kbScore">{score}</div>}
          </div>
        );
      })}
    </div>
  );
}
