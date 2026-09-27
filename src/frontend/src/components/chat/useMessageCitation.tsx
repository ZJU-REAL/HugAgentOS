import { useCallback } from 'react';
import { getCitationOutputSlice } from '../../utils/citations';
import { KBAssetThumbs } from '../kb/KBAssetThumbs';
import { extractAssetRefs, type KBAssetRef } from '../kb/kbAssets';
import { DataView } from '../tool/renderers/DataView';
import { CitedTextBody } from '../common/CitedTextBody';
import { useUIStore } from '../../stores';
import type { ChatMessage, CitationItem } from '../../types';
import { t } from '../../i18n';
function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? value as Record<string, unknown> : {};
}

export function useMessageCitation(m: ChatMessage) {
  const setDetailModal = useUIStore((s) => s.setDetailModal);
  /** Open citation detail */
  const handleCitationAction = useCallback((citation: CitationItem) => {
    const toolCalls = m.toolCalls;
    const { toolName, output } = getCitationOutputSlice(citation, toolCalls);

    if (toolName === 'internet_search') {
      const data = record(output);
      const searchResult = record(data.result ?? data);
      const results: unknown[] = Array.isArray(searchResult?.results) ? searchResult.results : [];
      const first = record(results[0]);
      const targetUrl = String(first?.url || citation.url || '');
      if (targetUrl) {
        window.open(targetUrl, '_blank', 'noopener,noreferrer');
        return;
      }
      const title = String(first?.title || citation.title || t('互联网搜索结果'));
      const content = String(first?.content || first?.snippet || citation.snippet || t('暂无内容'));
      setDetailModal({
        title,
        body: <CitedTextBody className="jx-tr-detailBody" text={content} highlight={citation.snippet} />,
      });
      return;
    }

    if (toolName === 'retrieve_dataset_content') {
      const data = record(output);
      const item = record(Array.isArray(data.items) ? data.items[0] : undefined);
      const docName = String(item?.['文件名称'] || item?.title || item?.document_name || citation.title || t('未知文档'));
      const content = String(item?.['文件内容'] || item?.content || citation.snippet || '');
      setDetailModal({
        title: docName,
        body: content
          ? <CitedTextBody className="jx-tr-detailBody" text={content} highlight={citation.snippet} />
          : <div className="jx-tr-detailBody">{t('暂无内容')}</div>,
      });
      return;
    }

    if (toolName === 'retrieve_local_kb') {
      const data = record(output);
      const item = record(Array.isArray(data.items) ? data.items[0] : undefined);
      const docName = String(item?.title || citation.title || t('未知文档'));
      const content = String(item?.content || citation.snippet || '');
      // 命中片段配的图（版面切出的图表 / 单独上传的图片）。检索结果带回结构化 images，
      // 历史命中没有时退回从正文里抠资产链接——只显示个文件名等于把图丢了。
      const assets: KBAssetRef[] = Array.isArray(item?.images) && item.images.length
        ? item.images as KBAssetRef[]
        : extractAssetRefs(content);
      setDetailModal({
        title: docName,
        body: (
          <>
            {content
              ? <CitedTextBody className="jx-tr-detailBody" text={content} highlight={citation.snippet} />
              : assets.length === 0 && <div className="jx-tr-detailBody">{t('暂无内容')}</div>}
            {assets.length > 0 && <KBAssetThumbs assets={assets} />}
          </>
        ),
      });
      return;
    }

    // 兜底：拿得到这次工具调用的**完整**结果就展示完整结果，并把引用片段定位高亮。
    // 以前这里只贴 citation.snippet —— 整体型锚点的 snippet 是后端截的前 500 字，
    // 于是「引用的是结果末尾、打开却只有开头」，等于没打开。
    const title = citation.title || t('引用详情');
    const outputText = typeof output === 'string' ? output : '';
    // 「比 snippet 更全」才值得换掉 snippet —— 取不到本次工具结果时
    // getCitationOutputSlice 会拿 snippet 造一份兜底，那种情况没有增量信息
    const hasFullOutput = (typeof output === 'object' && output !== null)
      || outputText.length > (citation.snippet || '').length;
    if (hasFullOutput) {
      setDetailModal({
        title,
        body: typeof output === 'string' && !/^\s*[[{]/.test(output)
          ? <CitedTextBody className="jx-tr-detailBody" text={output} highlight={citation.snippet} />
          : <DataView value={output} focus={citation.snippet} maxHeight={520} />,
      });
      return;
    }
    const snippet = citation.snippet || t('暂无内容');
    setDetailModal({ title, body: <div className="jx-tr-detailBody">{snippet}</div> });
  }, [m.toolCalls, setDetailModal]);

  return handleCitationAction;
}
