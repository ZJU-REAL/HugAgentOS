import { useEffect, useMemo } from 'react';
import { Alert, Button, Input, Modal, Popconfirm, message } from 'antd';
import { useStore } from 'zustand';
import { getDeviceSkillFile, putDeviceSkillFile, type DeviceCapabilityItem } from '../../api';
import { createDeviceSkillEditorStore } from '../../stores/deviceSkillEditorState';
import { useDesktopCapabilityStore } from '../../stores/desktopCapabilityStore';
import { ApiResponseError } from '../../utils/apiError';
import { t } from '../../i18n';

export function DeviceSkillEditor({ item, onClose }: { item: DeviceCapabilityItem; onClose: () => void }) {
  const store = useMemo(() => createDeviceSkillEditorStore(item.install_id,
    { read: getDeviceSkillFile, write: putDeviceSkillFile },
    async () => {
      await useDesktopCapabilityStore.getState().load('skill', true);
      message.success(t('本机技能副本已保存'));
    }), [item.install_id]);
  const state = useStore(store);
  useEffect(() => { void store.getState().load(); return () => store.getState().dispose(); }, [store]);
  const dirty = state.file !== null && state.content !== state.file.content;
  const reload = <Button loading={state.loading} onClick={dirty ? undefined : () => { void state.load(); }}>{t('重新加载')}</Button>;
  return <Modal title={t('编辑本机技能副本')} open onCancel={onClose} width={820}
    onOk={() => { void state.save(); }} okText={t('保存')} cancelText={t('取消')}
    confirmLoading={state.loading} okButtonProps={{ disabled: !state.file || !dirty || (state.error instanceof ApiResponseError && state.error.status === 409) }}>
    <p>{item.display_name || item.runtime_name} · SKILL.md</p>
    <p>{t('修改仅保存到这台设备的副本，云端原版不受影响。')}</p>
    {state.error != null && <Alert type="error" showIcon title={(state.error as Error).message}
      description={state.error instanceof ApiResponseError && state.error.status === 409
        ? t('技能已被其他操作修改，当前输入已保留。请重新加载最新版本后再编辑。') : undefined} />}
    <div className="jx-devcap-toolbar">
      {dirty ? <Popconfirm title={t('重新加载会替换当前未保存的内容，是否继续？')}
        okText={t('重新加载')} cancelText={t('取消')} onConfirm={() => state.load()}>{reload}</Popconfirm> : reload}
    </div>
    <Input.TextArea aria-label="SKILL.md" value={state.content} disabled={state.loading || !state.file}
      onChange={(event) => state.setContent(event.target.value)} rows={18} spellCheck={false} />
  </Modal>;
}
