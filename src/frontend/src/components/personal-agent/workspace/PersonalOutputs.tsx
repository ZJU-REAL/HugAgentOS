import { useEffect, useState } from 'react';
import { Alert, Button, Drawer } from 'antd';
import { personalApi, type OutputPage, type PersonalOutput, errorText } from '../api';
import { MonitorFileRow } from '../../monitor/MonitorFileRow';
import { fileTabId, useCanvasStore, type CanvasArtifact } from '../../../stores/canvasStore';
import { t } from '../../../i18n';

/** An output as the canvas takes it: the same row as the chat's file list opens the same canvas. */
function artifact(file: PersonalOutput): CanvasArtifact {
  return { file_id: file.id, name: file.name.split(/[\\/]/).pop() || file.name, url: file.url, mime_type: file.mime_type, size: file.size_bytes, origin: 'cloud' };
}

function OutputRows({ items }: { items: PersonalOutput[] }) {
  const activeTabId = useCanvasStore((s) => (s.isOpen ? s.activeTabId : null));
  return <div className="pa-output-rows">{items.map(item => {
    const file = artifact(item);
    return <MonitorFileRow key={file.file_id} file={file} active={activeTabId === fileTabId(file)} />;
  })}</div>;
}

function OutputHistory({ onOpen }: { onOpen: () => void }) {
  const [page, setPage] = useState<OutputPage>(); const [items, setItems] = useState<PersonalOutput[]>([]); const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  useEffect(() => { const controller = new AbortController(); void personalApi.outputs(undefined, controller.signal).then(result => { setPage(result); setItems(result.items); }).catch(exc => { if (!controller.signal.aborted) setError(errorText(exc)); }); return () => controller.abort(); }, []);
  async function more() {
    setBusy(true);
    try { const result = await personalApi.outputs(items[items.length - 1].id); setPage(result); setItems(previous => [...previous, ...result.items.filter(item => !previous.some(old => old.id === item.id))]); }
    catch (exc) { setError(errorText(exc)); } finally { setBusy(false); }
  }
  // Opening a file shows it in the canvas beside the conversation, so the drawer gets out of the way.
  return <div className="pa-outputs" onClick={event => { if ((event.target as HTMLElement).closest('.jx-runMonitor-row')) onOpen(); }}>
    {error && <Alert type="error" message={error} />}
    <OutputRows items={items} />
    {page?.has_more && <Button loading={busy} onClick={() => void more()}>{t('查看更多输出')}</Button>}
  </div>;
}

/** Saved outputs as one-line rows, like the chat's file list; the newest page here, the full history in a drawer. */
export function PersonalOutputs({ page }: { page?: OutputPage }) {
  const [open, setOpen] = useState(false);
  const items = page?.items || [];
  if (!items.length) return null;
  return <div className="pa-outputs">
    <OutputRows items={items} />
    {(page?.total || 0) > items.length && <button type="button" className="pa-side-more" onClick={() => setOpen(true)}>{t('查看全部输出')} ({page?.total})</button>}
    <Drawer open={open} title={t('输出')} onClose={() => setOpen(false)} destroyOnHidden width={600}>{open && <OutputHistory onOpen={() => setOpen(false)} />}</Drawer>
  </div>;
}
