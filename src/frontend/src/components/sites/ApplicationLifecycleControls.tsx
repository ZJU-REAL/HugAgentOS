import { useState } from 'react';
import { Button, Modal, Select, Space, message } from 'antd';
import { t } from '../../i18n';
import { applicationRequest, type Target } from './applicationApi';
export function ApplicationLifecycleControls({ appId, tableName, target, disabled, changed, onBusyChange }: {
 appId: string; tableName: string; target: Target; disabled: boolean; changed: () => Promise<void>; onBusyChange: (value: boolean) => void;
}) {
 const [busy, setBusy] = useState(false);
 const [history, setHistory] = useState<{ id: string; created_at: string }[] | null>(null);
 const [snapshot, setSnapshot] = useState<string>();
 const [revision, setRevision] = useState<string>();
 const root = '/v1/applications/' + appId;
 const perform = async (work: () => Promise<unknown>) => {
   setBusy(true); onBusyChange(true);
   try { await work(); setHistory(null); await changed(); }
   catch (error) { message.error(error instanceof Error ? error.message : t('操作失败')); }
   finally { setBusy(false); onBusyChange(false); }
 };
 const remove = (table: boolean) => Modal.confirm({
   title: table ? t('删除数据表') : t('删除应用'), content: t('此操作永久删除数据及恢复历史，无法撤销。'),
   okButtonProps: { danger: true }, onOk: () => perform(() => applicationRequest(
     root + (table ? '/tables/' + tableName : ''), { method: 'DELETE' }, target)),
 });
 const loadHistory = async () => {
   setBusy(true); onBusyChange(true);
   try {
     const [data, current] = await Promise.all([
       applicationRequest<{ items: { id: string; created_at: string }[] }>(root + '/tables/' + tableName + '/history', {}, target),
       applicationRequest<{ revision: string }>(root + '/tables/' + tableName + '/collection', {}, target),
     ]);
     setRevision(current.revision);
     setSnapshot(undefined); setHistory(data.items);
   } catch (error) { message.error(error instanceof Error ? error.message : t('操作失败')); }
   finally { setBusy(false); onBusyChange(false); }
 };
 return <Space wrap>
   <Button disabled={disabled || busy || !tableName} onClick={() => void loadHistory()}>{t('恢复历史版本')}</Button>
   <Button danger disabled={disabled || busy || !tableName} onClick={() => remove(true)}>{t('删除数据表')}</Button>
   <Button danger disabled={disabled || busy} onClick={() => remove(false)}>{t('删除应用')}</Button>
   <Modal title={t('恢复历史版本')} open={history !== null} onCancel={() => setHistory(null)}
     confirmLoading={busy} okButtonProps={{ disabled: !snapshot || busy }} onOk={() => void perform(async () => {
       await applicationRequest(root + '/tables/' + tableName + '/history/' + snapshot + '/restore',
         { method: 'POST', body: JSON.stringify({ revision }) }, target);
     })}>
     <p>{t('恢复会替换当前数据，并生成新的记录编号。当前版本会保留在历史中。')}</p>
     <Select style={{ width: '100%' }} value={snapshot} onChange={setSnapshot}
       options={(history ?? []).map(item => ({ value: item.id, label: item.created_at }))} />
   </Modal>
 </Space>;
}