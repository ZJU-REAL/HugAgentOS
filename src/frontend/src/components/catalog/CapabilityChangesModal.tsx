import { useEffect, useRef, useState } from 'react';
import { Alert, Checkbox, Collapse, Input, Modal, Select, Spin, Tag, message } from 'antd';
import {
  previewCapabilityChanges, commitCapabilityChanges,
  type CapabilityChangePreview, type CapabilityChangeChoices,
} from '../../api';
import { t } from '../../i18n';
import { useDesktopCapabilityStore } from '../../stores/desktopCapabilityStore';
import { useAuthStore } from '../../stores/authStore';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';

export function CapabilityChangesModal({ installId, onClose }: { installId: string; onClose: () => void }) {
  const [preview, setPreview] = useState<CapabilityChangePreview>();
  const [choices, setChoices] = useState<CapabilityChangeChoices>({});
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [acknowledge, setAcknowledge] = useState(false);
  const [forkKey, setForkKey] = useState('');
  const userId = useAuthStore((s) => s.authUser?.user_id);
  const serverBase = useDeploymentModeStore((s) => s.serverBase);
  const initialIdentity = useRef({ userId, serverBase });
  const alive = useRef(true);
  useEffect(() => {
    let cancelled = false;
    alive.current = true;
    void previewCapabilityChanges(installId).then((value) => {
      if (!cancelled) setPreview(value);
    }).catch((e: unknown) => { if (!cancelled) setError(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; alive.current = false; };
  }, [installId]);
  useEffect(() => {
    if (initialIdentity.current.userId !== userId || initialIdentity.current.serverBase !== serverBase) onClose();
  }, [userId, serverBase, onClose]);
  const needsFork = preview && !preview.can_edit;
  const sensitive = [...new Set([...(preview?.sensitive_paths ?? []), ...Object.entries(choices)
    .filter(([, choice]) => /-----BEGIN .*PRIVATE KEY-----|(?:api[_-]?key|token|password|secret|authorization)["']?\s*[=:]\s*\S+/i.test(choice.content ?? ''))
    .map(([path]) => path)])];
  const unresolved = preview?.changes.some((row) => row.conflict && !choices[row.path]);
  const submit = async () => {
    if (!preview) return;
    setBusy(true); setError('');
    try {
      const result = await commitCapabilityChanges({
        preview_id: preview.preview_id, choices, acknowledge_sensitive: acknowledge,
        ...(forkKey.trim() ? { fork_key: forkKey.trim() } : {}),
      });
      if (!alive.current) return;
      if (!result.applied) message.info(t('文件已保存到云端，尚未启用为云端能力'));
      else if (!result.local_applied && !forkKey.trim()) message.info(t('云端已保存，本地内容未自动应用'));
      else message.success(t('已提交到云端'));
      useDesktopCapabilityStore.getState().reloadAll();
      onClose();
    } catch (e) { if (alive.current) setError(e instanceof Error ? e.message : String(e)); }
    finally { if (alive.current) setBusy(false); }
  };
  return <Modal open title={t('能力文件变更')} width={860} onCancel={busy ? undefined : onClose}
    closable={!busy} maskClosable={!busy} confirmLoading={busy}
    okText={t('提交到云端')} cancelText={t('取消')} onOk={() => { void submit(); }}
    okButtonProps={{ disabled: !preview || (needsFork ? !/^[a-z0-9][a-z0-9_-]{0,62}$/.test(forkKey) : !!unresolved)
      || (!!sensitive.length && !acknowledge) }}>
    {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
    {!preview && !error && <Spin />}
    {preview && <>
      {needsFork && <Input aria-label={t('私有云端副本标识')} placeholder={t('私有云端副本标识')}
        value={forkKey} onChange={(e) => setForkKey(e.target.value)} style={{ marginBottom: 12 }} />}
      {!preview.changes.length && <Alert type="success" message={t('本地与云端内容一致')} />}
      <Collapse items={preview.changes.map((row) => ({
        key: row.path,
        label: <span style={{ overflowWrap: 'anywhere' }}>{row.path} {row.conflict && <Tag color="warning">{t('冲突')}</Tag>}</span>,
        children: <>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 240px), 1fr))', gap: 12 }}>
            {(['local', 'cloud'] as const).map((side) => <div key={side} style={{ minWidth: 0 }}>
              <strong>{t(side === 'local' ? '本地版本' : '云端版本')}</strong>
              <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 220, overflow: 'auto', fontSize: 12 }}>
                {row[side] === null ? t('文件不存在') : row.binary ? t('二进制文件') : row[side]!.slice(0, 20000)}
              </pre>
            </div>)}
          </div>
          {!needsFork && <Select aria-label={t('选择文件版本')} value={choices[row.path]?.side}
            placeholder={t(row.conflict ? '请选择冲突处理方式' : '自动合并')} style={{ minWidth: 200 }}
            options={[
              { value: 'local', label: t('使用本地版本') }, { value: 'cloud', label: t('使用云端版本') },
              ...(row.local !== null && row.cloud !== null ? [{ value: 'both', label: t('两份都保留') }] : []),
              ...(!row.binary ? [{ value: 'manual', label: t('手动合并') }] : []),
            ]}
            onChange={(side) => setChoices((old) => ({ ...old, [row.path]: { side, ...(side === 'manual' ? { content: row.local ?? row.cloud ?? '' } : {}) } }))} />}
          {choices[row.path]?.side === 'both' && <Input style={{ marginTop: 8 }}
            aria-label={t('本地副本新路径')} placeholder={t('本地副本新路径')}
            value={choices[row.path].alternate_path ?? ''}
            onChange={(e) => setChoices((old) => ({ ...old, [row.path]: { ...old[row.path], alternate_path: e.target.value } }))} />}
          {choices[row.path]?.side === 'manual' && <Input.TextArea style={{ marginTop: 8 }}
            aria-label={t('合并内容')} rows={8} value={choices[row.path].content ?? ''}
            onChange={(e) => setChoices((old) => ({ ...old, [row.path]: { ...old[row.path], content: e.target.value } }))} />}
        </>,
      }))} />
      {!!sensitive.length && <div style={{ marginTop: 12 }}>
        <Alert type="warning" message={t('以下文件疑似包含敏感内容')}
          description={sensitive.join(', ')} />
        <Checkbox checked={acknowledge} onChange={(e) => setAcknowledge(e.target.checked)}>
          {t('我确认将这些文件一并上传到云端')}
        </Checkbox>
      </div>}
    </>}
  </Modal>;
}
