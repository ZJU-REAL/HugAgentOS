import { useEffect, useState } from 'react';
import { Alert, Button, Drawer } from 'antd';
import { CodeOutlined, DownOutlined, FileImageOutlined, FileOutlined, FileTextOutlined, UpOutlined } from '@ant-design/icons';
import { personalApi, type OutputPage, type PersonalOutput, errorText } from '../api';
import { FilePreviewModal } from '../../file/FilePreviewModal';
import { ArtifactFileAction } from '../../file/ArtifactFileAction';
import type { ResourceItem } from '../../../types';
import { t } from '../../../i18n';

const VISIBLE = 3;

function resource(file: PersonalOutput): ResourceItem {
  return { id: file.id, file_id: file.id, origin: 'cloud', name: file.name, mime_type: file.mime_type, size: file.size_bytes, created_at: file.created_at, type: file.mime_type.startsWith('image/') ? 'image' : 'document' };
}

function FileIcon({ file }: { file: PersonalOutput }) {
  if (file.mime_type.startsWith('image/')) return <FileImageOutlined />;
  if (/json|javascript|xml|yaml|x-python|x-sh/.test(file.mime_type) || /\.(json|ya?ml|py|js|ts|sh|csv)$/i.test(file.name)) return <CodeOutlined />;
  if (/text|pdf|word|document|markdown|sheet|presentation/.test(file.mime_type)) return <FileTextOutlined />;
  return <FileOutlined />;
}

function Files({ items }: { items: PersonalOutput[] }) {
  const [preview, setPreview] = useState<ResourceItem | null>(null);
  return <>
    <ul className="pa-outputs">{items.map(file => <li key={file.id} className="pa-output">
      <button type="button" className="pa-output-name" onClick={() => setPreview(resource(file))} title={file.name}><FileIcon file={file} /><span>{file.name}</span></button>
      <span className="pa-output-action"><ArtifactFileAction file={resource(file)} /></span>
    </li>)}</ul>
    <FilePreviewModal item={preview} onClose={() => setPreview(null)} />
  </>;
}

function OutputHistory() {
  const [page, setPage] = useState<OutputPage>(); const [items, setItems] = useState<PersonalOutput[]>([]); const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  useEffect(() => { const controller = new AbortController(); void personalApi.outputs(undefined, controller.signal).then(result => { setPage(result); setItems(result.items); }).catch(exc => { if (!controller.signal.aborted) setError(errorText(exc)); }); return () => controller.abort(); }, []);
  async function more() {
    setBusy(true);
    try { const result = await personalApi.outputs(items[items.length - 1].id); setPage(result); setItems(previous => [...previous, ...result.items.filter(item => !previous.some(old => old.id === item.id))]); }
    catch (exc) { setError(errorText(exc)); } finally { setBusy(false); }
  }
  return <>{error && <Alert type="error" message={error} />}<Files items={items} />{page?.has_more && <Button loading={busy} onClick={() => void more()}>{t('查看更多输出')}</Button>}</>;
}

/** Saved outputs: three newest, expand to the latest page, then the full history drawer. */
export function PersonalOutputs({ page }: { page?: OutputPage }) {
  const [expanded, setExpanded] = useState(false);
  const [open, setOpen] = useState(false);
  const items = page?.items || [];
  if (!items.length) return <p className="pa-side-empty">{t('任务导出的文件会显示在这里。')}</p>;
  return <>
    <Files items={expanded ? items : items.slice(0, VISIBLE)} />
    {items.length > VISIBLE && <button type="button" className="pa-side-more" onClick={() => setExpanded(value => !value)}>{expanded ? <UpOutlined /> : <DownOutlined />} {expanded ? t('收起') : t('展开更多')}</button>}
    {expanded && (page?.total || 0) > items.length && <button type="button" className="pa-side-more" onClick={() => setOpen(true)}>{t('查看全部输出')} ({page?.total})</button>}
    <Drawer open={open} title={t('输出')} onClose={() => setOpen(false)} destroyOnHidden width={600}>{open && <OutputHistory />}</Drawer>
  </>;
}
