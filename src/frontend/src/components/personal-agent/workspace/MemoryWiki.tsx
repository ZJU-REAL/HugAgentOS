import { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Empty, Input, Tag } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { personalApi, errorText, type MemoryLint, type MemoryPage, type MemoryPages } from '../api';
import { LoadError, SkeletonLines } from '../../common/LoadState';
import { t } from '../../../i18n';
import { FindingList, MemoryPageView, PageBody } from './MemoryPageView';
import { PAGE_TYPES } from './memoryLabels';

interface MemoryWikiProps {
  /** The page to show, as /workspace/memory/<slug>.md; null shows the overview. */
  selected: string | null;
  onSelect: (path: string | null) => void;
  onHistory: (path: string) => void;
  onSource: (id: string) => void;
}

function PageItem({ page, active, onClick }: { page: MemoryPage; active: boolean; onClick: () => void }) {
  const archived = page.status === 'archived';
  return <li>
    <button type="button" className={'pa-mem-item' + (active ? ' is-active' : '') + (archived ? ' is-archived' : '')}
      aria-current={active ? 'page' : undefined} onClick={onClick}>
      <span className="pa-mem-itemName">
        <span>{page.name}</span>
        {page.pending && <Tag variant="filled" color="processing" className="pa-mem-itemTag">{t('待整理')}</Tag>}
        {archived && <Tag variant="filled" className="pa-mem-itemTag">{t('已归档')}</Tag>}
        {page.moved_to && <Tag variant="filled" className="pa-mem-itemTag">{t('已合并')}</Tag>}
      </span>
      {page.description && <span className="pa-mem-itemDesc">{page.description}</span>}
      <span className="pa-mem-itemMeta">{t('{n} 个来源', { n: page.sources })}{page.updated ? ' · ' + page.updated : ''}</span>
    </button>
  </li>;
}

/** The wiki: Where to start, pages grouped by type, and the page viewer. */
export function MemoryWiki({ selected, onSelect, onHistory, onSource }: MemoryWikiProps) {
  const [data, setData] = useState<MemoryPages | null>(null);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [dirty, setDirty] = useState(false);
  const [leaving, setLeaving] = useState<string | null | undefined>(undefined);
  const [lint, setLint] = useState<MemoryLint | null>(null);
  const [checking, setChecking] = useState(false);
  const [lintError, setLintError] = useState('');

  const check = async () => {
    setChecking(true); setLintError('');
    try { setLint(await personalApi.memoryLint()); }
    catch (exc) { setLintError(errorText(exc)); }
    finally { setChecking(false); }
  };

  // Bumped to read the pages again (refresh, after a save).
  const [revision, setRevision] = useState(0);
  const load = useCallback(() => setRevision((n) => n + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    personalApi.memoryPages(controller.signal)
      .then((next) => { setData(next); setError(''); })
      .catch((exc: unknown) => { if (!controller.signal.aborted) setError(errorText(exc)); });
    return () => controller.abort();
  }, [revision]);

  const pages = useMemo(() => data?.pages ?? [], [data]);
  const known = useMemo(() => new Set(pages.map((p) => p.name)), [pages]);
  const filtered = useMemo(() => {
    const text = query.trim().toLowerCase();
    if (!text) return pages;
    return pages.filter((p) => [p.name, p.description, ...p.aliases].some((value) => value.toLowerCase().includes(text)));
  }, [pages, query]);
  const groups = useMemo(() => {
    const keys = PAGE_TYPES.map((type) => type.key);
    const out = PAGE_TYPES.map((type) => ({ ...type, pages: filtered.filter((p) => p.type === type.key) }));
    const other = filtered.filter((p) => !keys.includes(p.type));
    if (other.length) out.push({ key: 'other', label: '其他', pages: other });
    // Active pages first, archived pages last within a group.
    for (const group of out) group.pages.sort((a, b) => Number(a.status === 'archived') - Number(b.status === 'archived') || a.name.localeCompare(b.name));
    return out.filter((group) => group.pages.length > 0);
  }, [filtered]);
  const pendingCount = pages.filter((p) => p.pending).length;

  const go = useCallback((path: string | null) => {
    if (path === selected) return;
    if (dirty) { setLeaving(path); return; }
    onSelect(path);
  }, [dirty, onSelect, selected]);
  const openSlug = useCallback((slug: string) => go('/workspace/memory/' + slug + '.md'), [go]);
  const handleDirty = useCallback((value: boolean) => setDirty(value), []);

  if (error) return <LoadError title={t('记忆加载失败')} reason={error} onRetry={() => load()} />;
  if (!data) return <SkeletonLines rows={8} />;
  if (!data.available) return <Empty description={t('当前部署未配置个人智能体工作区，请联系管理员设置 PERSONAL_AGENT_VOLUME_ROOT。')} />;

  const current = pages.find((p) => p.path === selected);
  const startHandlers = { known, onOpen: openSlug, onSource };

  return <div className="pa-mem-wiki">
    <aside className="pa-mem-nav" aria-label={t('记忆页面')}>
      <div className="pa-mem-navHead">
        <Input.Search allowClear size="small" aria-label={t('筛选页面')} placeholder={t('例如：张三、预算')}
          value={query} onChange={(event) => setQuery(event.target.value)} />
        <Button size="small" type="text" icon={<ReloadOutlined />} aria-label={t('刷新')} onClick={() => load()} />
      </div>
      <button type="button" className={'pa-mem-item pa-mem-item--overview' + (selected === null ? ' is-active' : '')}
        aria-current={selected === null ? 'page' : undefined} onClick={() => go(null)}>
        <span className="pa-mem-itemName">{t('从这里开始')}</span>
        <span className="pa-mem-itemMeta">{t('共 {n} 页', { n: pages.length })}{pendingCount ? ' · ' + t('{n} 页待整理', { n: pendingCount }) : ''}</span>
      </button>
      {groups.map((group) => <section key={group.key} className="pa-mem-group">
        <h3 className="pa-mem-groupTitle">{t(group.label)} <span>{group.pages.length}</span></h3>
        <ul className="pa-mem-list">{group.pages.map((page) => <PageItem key={page.path} page={page}
          active={page.path === selected} onClick={() => go(page.path)} />)}</ul>
      </section>)}
      {pages.length > 0 && filtered.length === 0 && <p className="pa-mem-hint pa-mem-pad">{t('没有匹配的页面')}</p>}
    </aside>

    <section className="pa-mem-main">
      {data.busy && <Alert className="pa-mem-banner" type="info" showIcon title={t('Dreaming 正在整理记忆，结束前不能保存修改。')} />}
      {leaving !== undefined && <Alert className="pa-mem-banner" type="warning" showIcon title={t('这个页面有未保存的修改。')}
        action={<span className="pa-mem-confirmActions">
          <Button size="small" type="text" onClick={() => setLeaving(undefined)}>{t('继续编辑')}</Button>
          <Button size="small" danger onClick={() => { const next = leaving; setLeaving(undefined); setDirty(false); onSelect(next); }}>{t('放弃修改')}</Button>
        </span>} />}
      {selected ? <MemoryPageView key={selected} path={selected} known={known} pending={current?.pending} busy={data.busy}
        onOpen={openSlug} onSource={onSource} onHistory={onHistory} onDirty={handleDirty} onSaved={() => load()} />
        : <div className="pa-mem-overview">
          <h2>{t('从这里开始')}</h2>
          {data.where_to_start ? <PageBody text={data.where_to_start} handlers={startHandlers} />
            : <p className="pa-mem-hint">{pages.length
              ? t('Dreaming 第一次整理后，这里会列出最该先读的几页。')
              : t('还没有记忆页面。智能体在对话和任务里遇到值得记住的人、项目和做法时会写下来；每晚 Dreaming 会补漏并整理成目录。')}</p>}
          {pages.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('记忆还是空的')} />}
          {pendingCount > 0 && <Alert type="info" showIcon title={t('{n} 个新页面等待整理', { n: pendingCount })}
            description={t('白天新建的页面先列在“待整理”，下次 Dreaming 会把它们放进合适的分组。')} />}
          {pages.length > 0 && <section className="pa-mem-check" aria-label={t('检查记忆')}>
            <div className="pa-mem-cardHead">
              <p className="pa-mem-hint">{t('检查页面格式、来源标注、目录和断开的链接。')}</p>
              <Button size="small" loading={checking} onClick={() => void check()}>{t('检查一遍')}</Button>
            </div>
            {lintError && <Alert type="error" showIcon title={lintError} />}
            {lint && (lint.findings?.length ? <FindingList findings={lint.findings} />
              : <Alert type="success" showIcon title={t('没有发现问题')} />)}
          </section>}
        </div>}
    </section>
  </div>;
}
