import { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Empty, Select, Tag, message } from 'antd';
import { CloseOutlined, DownOutlined, ReloadOutlined, RightOutlined, RollbackOutlined } from '@ant-design/icons';
import { personalApi, errorText, type MemoryVersion, type MemoryVersionQuery } from '../api';
import { LoadError, SkeletonLines } from '../../common/LoadState';
import { t } from '../../../i18n';
import { lineDiff } from './memoryText';
import { WRITER_KEYS, fileLabel, formatTime, writerLabel } from './memoryLabels';

export interface ChangeFilter { path?: string; source_id?: string; writer?: string }

const PAGE_SIZE = 30;

function Diff({ before, after }: { before: string; after: string }) {
  const lines = useMemo(() => lineDiff(before, after), [before, after]);
  // Long unchanged runs collapse to their first and last two lines.
  const shown: ({ kind: 'gap'; count: number } | (typeof lines)[number])[] = [];
  let run: typeof lines = [];
  const flush = () => {
    if (run.length > 5) { shown.push(...run.slice(0, 2), { kind: 'gap', count: run.length - 4 }, ...run.slice(-2)); }
    else shown.push(...run);
    run = [];
  };
  for (const line of lines) {
    if (line.kind === 'same') { run.push(line); continue; }
    flush(); shown.push(line);
  }
  flush();
  if (!lines.some((line) => line.kind !== 'same')) return <p className="pa-mem-hint">{t('内容没有变化')}</p>;
  return <pre className="pa-mem-diff" aria-label={t('修改前后对比')}>{shown.map((line, index) => line.kind === 'gap'
    ? <span key={index} className="pa-mem-diffGap">{t('… {n} 行未改动 …', { n: line.count })}</span>
    : <span key={index} className={'pa-mem-diffLine pa-mem-diffLine--' + line.kind}>
      <span className="pa-mem-diffMark" aria-hidden="true">{line.kind === 'add' ? '+' : line.kind === 'del' ? '−' : ' '}</span>
      <span className="pa-mem-srOnly">{line.kind === 'add' ? t('新增') : line.kind === 'del' ? t('删除') : ''}</span>
      {line.text || ' '}
    </span>)}</pre>;
}

function ChangeRow({ item, onReverted, onPath, onSource }: { item: MemoryVersion; onReverted: () => void; onPath: (path: string) => void; onSource: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<MemoryVersion | null>(null);
  const [error, setError] = useState('');
  const [confirming, setConfirming] = useState(false);
  const [reverting, setReverting] = useState(false);
  const [revertError, setRevertError] = useState('');

  const toggle = async () => {
    const next = !open;
    setOpen(next);
    if (next && !detail) {
      setError('');
      try { setDetail(await personalApi.memoryVersion(item.id)); }
      catch (exc) { setError(errorText(exc)); }
    }
  };

  const revert = async () => {
    setReverting(true); setRevertError('');
    try {
      await personalApi.revertMemoryVersion(item.id);
      message.success(t('已撤销，撤销本身也记了一条修改'));
      setConfirming(false);
      onReverted();
    } catch (exc) { setRevertError(errorText(exc)); }
    finally { setReverting(false); }
  };

  const action = item.created ? t('新建') : item.deleted ? t('删除') : t('修改');
  return <li className={'pa-mem-change' + (item.reverted_by ? ' is-reverted' : '')}>
    <div className="pa-mem-changeHead">
      <button type="button" className="pa-mem-changeToggle" aria-expanded={open} onClick={() => void toggle()}>
        {open ? <DownOutlined aria-hidden="true" /> : <RightOutlined aria-hidden="true" />}
        <Tag variant="filled" className={'pa-mem-writer pa-mem-writer--' + item.writer}>{writerLabel(item.writer)}</Tag>
        <span className="pa-mem-changeAction">{action}</span>
      </button>
      <button type="button" className="pa-mem-link" onClick={() => onPath(item.file)}>{fileLabel(item.file)}</button>
      {item.source_id && <button type="button" className="pa-mem-chip" title={t('只看这个来源的修改')} onClick={() => onSource(item.source_id as string)}>{item.source_id}</button>}
      {item.reverted_by && <Tag variant="filled">{t('已撤销')}</Tag>}
      <span className="pa-mem-changeTime">{formatTime(item.created_at)}</span>
      {item.revertable && !confirming && <Button size="small" type="text" icon={<RollbackOutlined />} onClick={() => setConfirming(true)}>{t('撤销')}</Button>}
    </div>
    {confirming && <div className="pa-mem-confirm" role="group" aria-label={t('确认撤销')}>
      <span>{item.created ? t('撤销后这个页面会被删除。修改记录会保留，之后还能再改回来。') : t('文件会回到这次修改之前的样子。修改记录会保留，之后还能再改回来。')}</span>
      <span className="pa-mem-confirmActions">
        <Button size="small" type="text" onClick={() => { setConfirming(false); setRevertError(''); }}>{t('取消')}</Button>
        <Button size="small" danger type="primary" loading={reverting} onClick={() => void revert()}>{t('撤销这次修改')}</Button>
      </span>
      {revertError && <Alert className="pa-mem-confirmError" type="error" showIcon title={revertError} />}
    </div>}
    {open && <div className="pa-mem-changeBody">
      {error ? <LoadError title={t('修改内容加载失败')} reason={error} onRetry={() => { setDetail(null); setOpen(false); void toggle(); }} />
        : !detail ? <SkeletonLines rows={4} />
        : <Diff before={detail.before ?? ''} after={detail.after ?? ''} />}
      {!item.revertable && !item.reverted_by && <p className="pa-mem-hint">{t('这个文件之后又被改过，不能直接撤销；需要的话在页面里编辑原文。')}</p>}
    </div>}
  </li>;
}

interface MemoryChangesProps {
  filter: ChangeFilter;
  onFilter: (filter: ChangeFilter) => void;
  pageOptions: { value: string; label: string }[];
}

/** The write log: who changed which memory file, when and from which source; diff and revert. */
export function MemoryChanges({ filter, onFilter, pageOptions }: MemoryChangesProps) {
  const [items, setItems] = useState<MemoryVersion[] | null>(null);
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState('');
  const [loadingMore, setLoadingMore] = useState(false);

  const query = useCallback((n: number): MemoryVersionQuery => ({ ...filter, page: n, page_size: PAGE_SIZE }), [filter]);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const data = await personalApi.memoryVersions(query(1), signal);
      if (signal?.aborted) return;
      setItems(data.items ?? []); setHasMore(!!data.has_more); setPage(1); setError('');
    } catch (exc) { if (!signal?.aborted) setError(errorText(exc)); }
  }, [query]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const more = async () => {
    setLoadingMore(true);
    try {
      const data = await personalApi.memoryVersions(query(page + 1));
      setItems((prev) => {
        // New changes shift the pages while the owner reads: skip rows already shown.
        const seen = new Set((prev ?? []).map((item) => item.id));
        return [...(prev ?? []), ...(data.items ?? []).filter((item) => !seen.has(item.id))];
      }); setHasMore(!!data.has_more); setPage(page + 1);
    } catch (exc) { message.error(t('更早的修改加载失败：{reason}', { reason: errorText(exc) })); }
    finally { setLoadingMore(false); }
  };

  const options = useMemo(() => {
    const list = [...pageOptions];
    if (filter.path && !list.some((o) => o.value === filter.path)) list.push({ value: filter.path, label: fileLabel(filter.path) });
    return list;
  }, [pageOptions, filter.path]);

  return <div className="pa-mem-changes">
    <div className="pa-mem-filters">
      <label className="pa-mem-field"><span>{t('文件')}</span>
        <Select size="small" allowClear showSearch optionFilterProp="label" placeholder={t('全部文件')} value={filter.path}
          options={options} onChange={(value?: string) => onFilter({ ...filter, path: value || undefined })} className="pa-mem-select" />
      </label>
      <label className="pa-mem-field"><span>{t('写入者')}</span>
        <Select size="small" allowClear placeholder={t('全部')} value={filter.writer} className="pa-mem-select pa-mem-select--short"
          options={WRITER_KEYS.map((key) => ({ value: key, label: writerLabel(key) }))}
          onChange={(value?: string) => onFilter({ ...filter, writer: value || undefined })} />
      </label>
      {filter.source_id && <Tag className="pa-mem-filterTag" closable closeIcon={<CloseOutlined aria-label={t('清除来源筛选')} />}
        onClose={() => onFilter({ ...filter, source_id: undefined })}>{t('来源：{id}', { id: filter.source_id })}</Tag>}
      <Button size="small" type="text" icon={<ReloadOutlined />} aria-label={t('刷新')} onClick={() => void load()} />
    </div>
    {error ? <LoadError title={t('修改记录加载失败')} reason={error} onRetry={() => void load()} />
      : !items ? <SkeletonLines rows={6} />
      : items.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={filter.path || filter.writer || filter.source_id
        ? t('没有符合筛选的修改') : t('还没有修改记录。智能体、Dreaming 或你改动记忆后，每一次都会记在这里，保留 30 天。')} />
      : <ul className="pa-mem-changeList">{items.map((item) => <ChangeRow key={item.id} item={item} onReverted={() => void load()}
        onPath={(path) => onFilter({ ...filter, path })} onSource={(id) => onFilter({ ...filter, source_id: id })} />)}</ul>}
    {hasMore && <div className="pa-mem-more"><Button size="small" loading={loadingMore} onClick={() => void more()}>{t('加载更早的修改')}</Button></div>}
  </div>;
}
