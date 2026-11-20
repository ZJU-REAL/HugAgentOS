import { useState, useEffect } from 'react';
import { CloseOutlined, SearchOutlined } from '@ant-design/icons';
import { Input } from 'antd';
import { useUIStore } from '../../stores';
import { useComposerDraft } from '../../stores/composerStore';
import { usePageConfigStore } from '../../stores/pageConfigStore';
import { useDelayedFlag } from '../../hooks';
import { t } from '../../i18n';

export function PromptHubPanel({ draftKey }: { draftKey: string }) {
  const { promptHubOpen, setPromptHubOpen } = useUIStore();
  const { setInput } = useComposerDraft(draftKey);
  // The prompt library comes with /content/docs, which the page-config store already caches (sorted).
  const items = usePageConfigStore((s) => s.promptHub);
  const loaded = usePageConfigStore((s) => s.loaded);
  const fetching = usePageConfigStore((s) => s.fetching);
  const loading = !loaded && fetching;
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState<number | null>(null);

  // Opened before the docs arrived (or after that request failed): ask once more.
  useEffect(() => {
    if (!promptHubOpen) return;
    const store = usePageConfigStore.getState();
    if (!store.loaded && !store.fetching) void store.fetchConfig();
  }, [promptHubOpen]);

  const showSkeleton = useDelayedFlag(loading);

  if (!promptHubOpen) return null;

  const keyword = search.trim().toLowerCase();
  const filtered = keyword
    ? items.filter(
        (p) =>
          p.title.toLowerCase().includes(keyword) ||
          p.content.toLowerCase().includes(keyword),
      )
    : items;

  const handleSelect = (idx: number) => {
    setSelected(idx);
    setInput(filtered[idx].content);
  };

  return (
    <div className="jx-promptHub">
      <div className="jx-promptHub-header">
        <div className="jx-promptHub-headerRow">
          <span className="jx-promptHub-title">{t('提示词中心')}</span>
          <button
            className="jx-trp-close"
            onClick={() => { setPromptHubOpen(false); setSearch(''); }}
            aria-label={t('关闭面板')}
          >
            <CloseOutlined />
          </button>
        </div>
        <Input
          prefix={<SearchOutlined style={{ color: '#B3B3B3' }} />}
          placeholder={t('搜索提示词...')}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          allowClear
          className="jx-promptHub-search"
        />
      </div>

      <div className="jx-promptHub-list">
        {showSkeleton ? (
          <div className="jx-promptHub-skeletonList" aria-hidden="true" aria-busy="true">
            {Array.from({ length: 5 }).map((_, idx) => (
              <div key={idx} className="jx-promptHub-card jx-promptHub-card--skeleton">
                <div className="jx-skeletonBlock jx-promptHub-skTitle" />
                <div className="jx-skeletonBlock jx-promptHub-skLine" />
                <div className="jx-skeletonBlock jx-promptHub-skLine jx-promptHub-skLine--short" />
              </div>
            ))}
          </div>
        ) : loading ? null : filtered.length === 0 ? (
          <div className="jx-promptHub-empty">
            {keyword ? t('没有匹配的提示词') : t('暂无提示词，请在管理后台添加')}
          </div>
        ) : (
          filtered.map((item, idx) => (
            <div
              key={`${item.title}-${idx}`}
              className={`jx-promptHub-card${selected === idx ? ' selected' : ''}`}
              onClick={() => handleSelect(idx)}
            >
              <div className="jx-promptHub-cardTitle">{item.title}</div>
              <div className="jx-promptHub-cardContent">{item.content}</div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
