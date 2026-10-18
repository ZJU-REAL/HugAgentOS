import { useEffect, useState } from 'react';
import { Alert, Breadcrumb, Button, Modal, Select, Space, Table, message } from 'antd';
import { FolderOutlined } from '@ant-design/icons';
import { useComposerDraft } from '../../stores/composerStore';
import { t } from '../../i18n';
import {
  listSpaceRoots, listSpaceEntries, referenceSpaceFile,
  type SpaceRoot, type SpaceEntry, type SpacePage,
} from '../myspace/spaceFilesApi';

interface Props {
  draftKey?: string;
  open: boolean;
  onClose: () => void;
  title?: string;
}

export function MySpaceImportModal({ draftKey, open, onClose, title }: Props) {
  const [roots, setRoots] = useState<SpaceRoot[]>([]);
  const [space, setSpace] = useState('');
  const [path, setPath] = useState('');
  const [page, setPage] = useState<SpacePage | null>(null);
  const [selected, setSelected] = useState<SpaceEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const { addImportedSpaceFiles } = useComposerDraft(draftKey);
  useEffect(() => {
    if (!open) return;
    const abort = new AbortController();
    setPath(''); setSelected([]); setError('');
    void listSpaceRoots(abort.signal).then(result => {
      setRoots(result.items); setSpace(result.items[0]?.space_id || '');
    }).catch(reason => { if (!abort.signal.aborted) setError(String(reason)); });
    return () => abort.abort();
  }, [open]);
  useEffect(() => {
    if (!open || !space) return;
    const abort = new AbortController();
    setLoading(true); setPage(null); setSelected([]);
    void listSpaceEntries(space, path, undefined, abort.signal).then(setPage)
      .catch(reason => { if (!abort.signal.aborted) setError(String(reason)); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [open, space, path]);
  async function submit() {
    setSubmitting(true);
    try {
      const files = [];
      for (const entry of selected) files.push(await referenceSpaceFile(space, entry.path));
      addImportedSpaceFiles(files);
      onClose();
    } catch (reason) { void message.error(String(reason)); }
    finally { setSubmitting(false); }
  }
  async function more() {
    if (!page?.next_cursor) return;
    setLoading(true);
    try {
      const next = await listSpaceEntries(space, path, page.next_cursor);
      setPage(current => current && {...next, entries:[...current.entries,...next.entries]});
    } catch (reason) { setError(String(reason)); }
    finally { setLoading(false); }
  }
  const parts = path.split('/').filter(Boolean);
  return <Modal open={open} title={title || t('从我的空间导入')} width={850}
    onCancel={onClose} onOk={() => void submit()} confirmLoading={submitting}
    okText={t('确认导入')} okButtonProps={{disabled: !selected.length || loading}}>
    {error && <Alert type="error" message={error} showIcon />}
    <Space style={{marginBottom:16}}>
      <Select value={space || undefined} style={{minWidth:220}}
        options={roots.map(root => ({value:root.space_id,label:root.name}))}
        onChange={value => {setSpace(value);setPath('');}} />
      <Breadcrumb items={[
        {title:<Button type="link" onClick={() => setPath('')}>{t('我的空间')}</Button>},
        ...parts.map((part,index) => ({title:<Button type="link"
          onClick={() => setPath(parts.slice(0,index+1).join('/'))}>{part}</Button>})),
      ]} />
    </Space>
    <Table<SpaceEntry> rowKey="path" dataSource={page?.entries || []} loading={loading}
      pagination={false}
      rowSelection={{selectedRowKeys:selected.map(item=>item.path),
        onChange:(_keys,rows)=>setSelected(rows),
        getCheckboxProps:item=>({disabled:item.is_directory || submitting})}}
      columns={[
        {title:t('名称'),dataIndex:'name',render:(_name,entry)=>entry.is_directory
          ? <Button type="link" icon={<FolderOutlined />} onClick={()=>setPath(entry.path)}>{entry.name}</Button>
          : entry.name},
        {title:t('大小'),dataIndex:'size',render:(size,entry)=>entry.is_directory?'—':size.toLocaleString()+' B'},
      ]} />
    {page?.next_cursor && <Button loading={loading} onClick={()=>void more()}>{t('加载更多')}</Button>}
  </Modal>;
}
