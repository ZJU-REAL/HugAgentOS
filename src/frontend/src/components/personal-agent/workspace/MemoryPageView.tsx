import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { Alert, Button, Input, Popover, Tag, message } from 'antd';
import { EditOutlined, HistoryOutlined, ReloadOutlined } from '@ant-design/icons';
import { personalApi, errorText, type MemoryLintFinding, type WorkspaceFile } from '../api';
import { LoadError, SkeletonLines } from '../../common/LoadState';
import { t } from '../../../i18n';
import { blocks, inline, isConflict, lintFindings, parsePage, type Block, type Inline } from './memoryText';
import { typeLabel } from './memoryLabels';

const SOURCE_KINDS: Record<string, string> = { conv: '对话', chan: '渠道会话', task: '后台任务' };

interface InlineHandlers {
  known: Set<string>;
  onOpen: (slug: string) => void;
  onSource?: (id: string) => void;
}

function SourceChip({ id, kind, asOf, onSource }: { id: string; kind: string | null; asOf: string; onSource?: (id: string) => void }) {
  const content = <div className="pa-mem-source">
    <dl>
      <dt>{t('来源')}</dt><dd><code>{id}</code></dd>
      <dt>{t('类型')}</dt><dd>{kind ? t(SOURCE_KINDS[kind]) + ' · ' + kind : t('未知')}</dd>
      <dt>{t('截至')}</dt><dd>{asOf}</dd>
    </dl>
    {onSource && <Button size="small" type="link" icon={<HistoryOutlined />} onClick={() => onSource(id)}>{t('查看这个来源写入的修改')}</Button>}
  </div>;
  return <Popover trigger="click" content={content}>
    <button type="button" className={'pa-mem-chip pa-mem-chip--' + (kind || 'other')} aria-label={t('来源 {id}', { id })}>
      {kind ? t(SOURCE_KINDS[kind]) : id} · {asOf}
    </button>
  </Popover>;
}

/** One line of page text with its marks: source chips, links, superseded values, observations. */
export function InlineText({ text, handlers }: { text: string; handlers: InlineHandlers }) {
  return <>{inline(text).map((part: Inline, index) => {
    switch (part.kind) {
      case 'source': return <SourceChip key={index} id={part.id} kind={part.sourceKind} asOf={part.asOf} onSource={handlers.onSource} />;
      case 'link': return handlers.known.has(part.slug)
        ? <button key={index} type="button" className="pa-mem-link" onClick={() => handlers.onOpen(part.slug)}>{part.slug}</button>
        : <span key={index} className="pa-mem-link pa-mem-link--missing" title={t('还没有这个页面')}>{part.slug}</span>;
      case 'superseded': return <span key={index} className="pa-mem-superseded" title={t('{date} 之前是这个值', { date: part.until })}>
        <span className="pa-mem-supersededLabel">{t('曾经是')}</span> <s>{part.value}</s> <span className="pa-mem-supersededLabel">{t('至 {date}', { date: part.until })}</span>
      </span>;
      case 'observation': return <Tag key={index} variant="filled" className="pa-mem-observation">{t('agent 观察')}</Tag>;
      case 'strong': return <strong key={index}>{part.text}</strong>;
      case 'code': return <code key={index}>{part.text}</code>;
      default: return <span key={index}>{part.text}</span>;
    }
  })}</>;
}

function BlockView({ block, handlers }: { block: Block; handlers: InlineHandlers }): ReactNode {
  if (block.kind === 'heading') {
    return block.level <= 2 ? <h3 className="pa-mem-h">{block.text}</h3> : <h4 className="pa-mem-h pa-mem-h--minor">{block.text}</h4>;
  }
  if (block.kind === 'stub') {
    return <Alert type="info" showIcon title={<span>{t('这个页面已合并到')} <InlineText text={'[[' + block.target + ']]'} handlers={handlers} /></span>} />;
  }
  if (block.kind === 'fact') {
    const body = block.conflict ? block.text.slice('CONFLICT:'.length).trim() : block.text;
    return <li className={'pa-mem-fact' + (block.conflict ? ' pa-mem-fact--conflict' : '') + (block.depth ? ' pa-mem-fact--d' + block.depth : '')}>
      {block.conflict && <Tag variant="filled" color="warning" className="pa-mem-conflictTag">{t('冲突')}</Tag>}
      <InlineText text={body} handlers={handlers} />
    </li>;
  }
  return <p className="pa-mem-p"><InlineText text={block.text} handlers={handlers} /></p>;
}

/** Page body: consecutive facts share one list. */
export function PageBody({ text, handlers }: { text: string; handlers: InlineHandlers }) {
  const items = blocks(text);
  const groups: (Block | Block[])[] = [];
  for (const block of items) {
    const last = groups[groups.length - 1];
    if (block.kind === 'fact' && Array.isArray(last)) last.push(block);
    else groups.push(block.kind === 'fact' ? [block] : block);
  }
  return <div className="pa-mem-body">{groups.map((group, index) => Array.isArray(group)
    ? <ul key={index} className="pa-mem-facts">{group.map((block, i) => <BlockView key={i} block={block} handlers={handlers} />)}</ul>
    : <BlockView key={index} block={group} handlers={handlers} />)}</div>;
}

export function FindingList({ findings }: { findings: MemoryLintFinding[] }) {
  return <ul className="pa-mem-findings">{findings.map((f, index) => <li key={index}>
    <Tag variant="filled" color={f.level === 'block' ? 'error' : 'warning'}>{f.level === 'block' ? t('必须修改') : t('提醒')}</Tag>
    <span>{f.detail}</span>
  </li>)}</ul>;
}

interface MemoryPageViewProps {
  path: string;
  known: Set<string>;
  pending?: boolean;
  busy?: boolean;
  onOpen: (slug: string) => void;
  onSource: (id: string) => void;
  onHistory: (path: string) => void;
  onDirty: (dirty: boolean) => void;
  onSaved: () => void;
}

/** A wiki page rendered for reading, with a raw editor that saves through the workspace file API. */
export function MemoryPageView({ path, known, pending, busy, onOpen, onSource, onHistory, onDirty, onSaved }: MemoryPageViewProps) {
  const [file, setFile] = useState<WorkspaceFile | null>(null);
  const [error, setError] = useState('');
  const [draft, setDraft] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<{ text: string; findings: MemoryLintFinding[]; conflict: boolean } | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try { const next = await personalApi.workspaceFile(path, signal); if (!signal?.aborted) { setFile(next); setError(''); } }
    catch (exc) { if (!signal?.aborted) setError(errorText(exc)); }
  }, [path]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  useEffect(() => { onDirty(draft !== null && draft !== (file?.content ?? '')); }, [draft, file, onDirty]);

  const save = async () => {
    if (!file || draft === null) return;
    setSaving(true); setSaveError(null);
    try {
      setFile(await personalApi.saveWorkspaceFile(file.path, draft, file.etag));
      setDraft(null);
      message.success(t('已保存，修改记录里可以撤销'));
      onSaved();
    } catch (exc) {
      setSaveError({ text: errorText(exc), findings: lintFindings(exc), conflict: isConflict(exc) });
    } finally { setSaving(false); }
  };

  if (error) return <LoadError title={t('页面加载失败')} reason={error} onRetry={() => void load()} />;
  if (!file) return <SkeletonLines rows={8} />;
  const parsed = parsePage(file.content ?? '');
  const handlers = { known, onOpen, onSource };
  const meta = parsed.meta;

  return <article className="pa-mem-page" aria-label={meta.name || path}>
    <header className="pa-mem-pageHead">
      <div className="pa-mem-pageTitle">
        <h2>{meta.name || path.split('/').pop()}</h2>
        <span className="pa-mem-pageTags">
          {meta.type && <Tag variant="filled">{typeLabel(meta.type)}</Tag>}
          {meta.status === 'archived' && <Tag variant="filled">{t('已归档')}</Tag>}
          {pending && <Tag variant="filled" color="processing">{t('待整理')}</Tag>}
        </span>
      </div>
      <span className="pa-mem-pageActions">
        <Button size="small" type="text" icon={<HistoryOutlined />} onClick={() => onHistory(file.path)}>{t('修改记录')}</Button>
        <Button size="small" type="text" icon={<ReloadOutlined />} aria-label={t('刷新')} onClick={() => void load()} />
        {file.editable && draft === null && <Button size="small" icon={<EditOutlined />} onClick={() => { setDraft(file.content ?? ''); setSaveError(null); }}>{t('编辑原文')}</Button>}
      </span>
    </header>
    {meta.description && <p className="pa-mem-desc">{meta.description}</p>}
    {(meta.aliases.length > 0 || meta.updated) && <p className="pa-mem-meta">
      {meta.aliases.length > 0 && <span>{t('别名：{names}', { names: meta.aliases.join('、') })}</span>}
      {meta.updated && <span>{t('更新于 {date}', { date: meta.updated })}</span>}
    </p>}

    {draft !== null ? <div className="pa-mem-editor">
      {busy && <Alert type="warning" showIcon title={t('Dreaming 正在整理记忆，现在保存会被拒绝；可以先改着，等它结束再保存。')} />}
      {saveError && <Alert type={saveError.conflict ? 'warning' : 'error'} showIcon title={saveError.text}
        description={saveError.findings.length ? <FindingList findings={saveError.findings} /> : undefined} />}
      <Input.TextArea className="pa-mem-textarea" aria-label={t('页面原文')} value={draft} spellCheck={false}
        autoSize={{ minRows: 14, maxRows: 32 }} onChange={(event) => setDraft(event.target.value)} />
      <div className="pa-mem-editorActions">
        <span className="pa-mem-hint">{t('每条事实写在 ## 小节下的 “- ” 行里，并带 [src: … | as-of: YYYY-MM-DD]。')}</span>
        <Button size="small" type="text" onClick={() => { setDraft(null); setSaveError(null); }}>{t('取消')}</Button>
        <Button size="small" type="primary" loading={saving} onClick={() => void save()}>{t('保存')}</Button>
      </div>
    </div> : <PageBody text={parsed.body} handlers={handlers} />}
  </article>;
}
