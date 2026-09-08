import { useEffect, useState } from 'react';
import { useStore } from 'zustand';
import { Alert, Button, Empty, Form, Input, Modal, Popconfirm, Select, Switch, message } from 'antd';
import {
  deleteDeviceLocalMcp, getDeviceMcpJson, putDeviceLocalMcp, repairDeviceMcpJson, setDeviceManagedMcpEnabled,
  type DeviceMcpJson,
} from '../../api';
import { t } from '../../i18n';
import { createDeviceMcpStore } from '../../stores/deviceMcpState';
import { useAuthStore } from '../../stores/authStore';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { useDesktopCapabilityStore } from '../../stores/desktopCapabilityStore';
import { ApiResponseError } from '../../utils/apiError';
import { deviceMcpSpec, type DeviceMcpFormValues } from '../../utils/deviceMcpForm';

function recoveryAction(error: unknown): string | undefined {
  if (!(error instanceof ApiResponseError) || !error.payload || typeof error.payload !== 'object') return;
  const body = error.payload as { detail?: { recovery_action?: string }; data?: { recovery_action?: string }; recovery_action?: string };
  return body.detail?.recovery_action || body.data?.recovery_action || body.recovery_action;
}

function accountKey(userId: string | undefined, mode: string | null | undefined, base: string | null | undefined) {
  return JSON.stringify([userId, mode, base]);
}
function currentAccountKey() {
  const deployment = useDeploymentModeStore.getState();
  return accountKey(useAuthStore.getState().authUser?.user_id, deployment.provisionMode, deployment.serverBase);
}

export function DeviceMcpManager() {
  const userId = useAuthStore((s) => s.authUser?.user_id);
  const mode = useDeploymentModeStore((s) => s.provisionMode);
  const base = useDeploymentModeStore((s) => s.serverBase);
  if (mode !== 'dual') return null;
  const identity = accountKey(userId, mode, base);
  // Remount clears doc, form, modal and errors before the new account renders.
  return <DeviceMcpAccountManager key={identity} identity={identity} />;
}

function DeviceMcpAccountManager({ identity }: { identity: string }) {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<string | null>(null); // 空串表示新增
  const [form] = Form.useForm<DeviceMcpFormValues>();
  const transport = Form.useWatch('transport', form);
  const [store] = useState(() => createDeviceMcpStore({
    read: getDeviceMcpJson,
    refresh: async () => { await useDesktopCapabilityStore.getState().load('mcp', true); },
  }, () => currentAccountKey() === identity));
  const { doc, error, busy } = useStore(store);
  const load = store.getState().load;
  useEffect(() => {
    if (open) void load();
    return () => store.getState().reset();
  }, [open, load, store]);

  const version = () => {
    if (!doc) throw new Error(t('请先重新加载本机连接器'));
    return { expected_generation: doc.generation, expected_digest: doc.digest };
  };
  const mutate = async (action: () => Promise<DeviceMcpJson>) => {
    try { return await store.getState().mutate(action); }
    catch (err) { message.error((err as Error).message); throw err; }
  };
  const edit = (id: string) => {
    const value = id ? doc?.local[id] : undefined;
    form.resetFields();
    form.setFieldsValue({
      server_id: id, transport: value?.transport || 'streamable_http',
      displayName: value?.displayName, description: value?.description,
      command: value?.command, args_text: value?.args?.join('\n'),
      cwd: value?.cwd, env_text: value?.env ? JSON.stringify(value.env, null, 2) : '',
      url: value?.url, secret_headers_text: '',
    });
    setEditing(id);
  };
  const save = async () => {
    try {
      const values = await form.validateFields();
      const previous = editing ? doc?.local[editing] : undefined;
      if (editing && !previous) throw new Error(t('该连接器已被移除，请重新添加'));
      const spec = deviceMcpSpec(values, previous?.enabled ?? true);
      if (previous?.executionTimeout !== undefined) spec.executionTimeout = previous.executionTimeout;
      const saved = await mutate(() => putDeviceLocalMcp(values.server_id.trim(), { ...spec, ...version() }));
      if (saved) setEditing(null);
    } catch (err) {
      if (err instanceof Error && !(err instanceof ApiResponseError)) message.error(err.message);
    }
  };
  const ignore = () => {};
  const repair = async () => {
    try {
      await mutate(async () => {
        await repairDeviceMcpJson();
        if (currentAccountKey() !== identity) throw new Error('account changed');
        return getDeviceMcpJson();
      });
    } catch { /* The current account error is already shown; old results are discarded. */ }
  };

  return <>
    <Button onClick={() => setOpen(true)}>{t('管理本机连接器')}</Button>
    <Modal title={t('管理本机连接器')} open={open} onCancel={() => { setOpen(false); setEditing(null); form.resetFields(); }}
      footer={null} width={760}>
      <p>{t('本机直连由此设备启动或访问；云端托管连接器通过账号授权调用。')}</p>
      {error != null && <Alert type="error" showIcon title={(error as Error).message}
        description={error instanceof ApiResponseError && error.status === 409
          ? t('配置已变化或损坏。表单内容已保留，请重新加载后检查并保存。') : undefined}
        action={recoveryAction(error) === 'repair_mcp_json' ? <Popconfirm
          title={t('保留损坏文件并重建连接器配置？')} description={t('原文件会移到备份位置，本机连接器需要重新配置。')}
          onConfirm={() => repair()} okText={t('修复')} cancelText={t('取消')}>
          <Button danger loading={busy}>{t('修复')}</Button>
        </Popconfirm> : undefined} />}
      <div className="jx-devcap-toolbar">
        <Button loading={busy} onClick={() => { void load(); }}>{t('重新加载')}</Button>
        <Button type="primary" disabled={!doc || busy} onClick={() => edit('')}>{t('添加本机连接器')}</Button>
      </div>
      <h4>{t('本机直连')}</h4>
      {doc && Object.keys(doc.local).length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('暂无本机连接器')} />}
      {doc && Object.entries(doc.local).map(([id, entry]) => <div className="jx-devcap-row" key={id}>
        <div className="jx-devcap-row-head"><strong>{entry.displayName || id}</strong>
          <span>{entry.transport}</span>
          <Switch aria-label={t('启用 {name}', { name: entry.displayName || id })} checked={entry.enabled} disabled={busy}
            onChange={(on) => { void mutate(() => putDeviceLocalMcp(id, { ...entry, enabled: on, ...version() })).catch(ignore); }} />
          <Button size="small" disabled={busy} onClick={() => edit(id)}>{t('编辑')}</Button>
          <Popconfirm title={t('删除这个本机连接器？')} okText={t('删除')} cancelText={t('取消')}
            onConfirm={() => mutate(() => deleteDeviceLocalMcp(id, version())).catch(ignore)}>
            <Button danger size="small" disabled={busy}>{t('删除')}</Button>
          </Popconfirm>
        </div>
        <div>{entry.description}</div>
        <small>{entry.credentialRef ? t('已保存认证信息') : t('未设置认证信息')}</small>
      </div>)}
      <h4>{t('云端托管')}</h4>
      {doc && Object.values(doc.managedProfiles).every((profile) => !Object.keys(profile.servers).length)
        && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('暂无云端托管连接器')} />}
      {doc && Object.entries(doc.managedProfiles).flatMap(([profile, value]) =>
        Object.entries(value.servers).map(([id, entry]) => <div className="jx-devcap-row" key={profile + ':' + id}>
          <div className="jx-devcap-row-head"><strong>{entry.displayName || id}</strong>
            <span>{t('云端')}</span>
            <Switch aria-label={t('启用 {name}', { name: entry.displayName || id })} checked={entry.enabled} disabled={busy}
              onChange={(on) => { void mutate(() => setDeviceManagedMcpEnabled(profile, id, on)).catch(ignore); }} />
          </div>
          <small>{t('仅影响此设备')}</small>
        </div>))}
    </Modal>
    <Modal title={editing ? t('编辑本机连接器') : t('添加本机连接器')} open={editing !== null}
      onCancel={() => { setEditing(null); form.resetFields(); }} onOk={() => { void save(); }}
      confirmLoading={busy} okButtonProps={{ disabled: !doc || error != null }} okText={t('保存')} cancelText={t('取消')}>
      {error != null && <Alert type="error" title={(error as Error).message}
        description={t('配置已变化或损坏。表单内容已保留，请重新加载后检查并保存。')}
        action={<Button loading={busy} onClick={() => { void load(); }}>{t('重新加载')}</Button>} />}
      <Form form={form} layout="vertical">
        <Form.Item name="server_id" label={t('连接器标识')} rules={[{ required: true }, { pattern: /^[A-Za-z0-9][A-Za-z0-9._-]*$/, message: t('使用字母、数字、点、下划线或短横线') }]}>
          <Input disabled={!!editing} />
        </Form.Item>
        <Form.Item name="displayName" label={t('名称')}><Input /></Form.Item>
        <Form.Item name="description" label={t('描述')}><Input.TextArea rows={2} /></Form.Item>
        <Form.Item name="transport" label={t('传输方式')} rules={[{ required: true }]}>
          <Select options={['streamable_http', 'sse', 'stdio'].map((value) => ({ value, label: value }))} />
        </Form.Item>
        {transport === 'stdio' ? <>
          <Form.Item name="command" label={t('启动命令')} rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="args_text" label={t('启动参数（每行一个）')}><Input.TextArea rows={3} /></Form.Item>
          <Form.Item name="cwd" label={t('工作目录')}><Input /></Form.Item>
          <Form.Item name="env_text" label={t('环境变量（JSON）')}><Input.TextArea rows={3} /></Form.Item>
        </> : <>
          <Form.Item name="url" label={t('服务地址')} rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="secret_headers_text" label={t('认证请求头（JSON，留空保持）')}><Input.TextArea rows={3} autoComplete="off" /></Form.Item>
        </>}
      </Form>
    </Modal>
  </>;
}
