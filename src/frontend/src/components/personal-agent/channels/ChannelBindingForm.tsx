import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Input, Radio, Spin } from 'antd';
import { createChannelBot, getWeixinBindStatus, listChannelAdapters, prepareChannelLocalBinding, startWeixinBind, type ChannelAdapterInfo } from '../../../api';
import { createWeixinBindingFlow, INITIAL_WEIXIN_BINDING } from '../../settings/weixinBindingFlow';
import { useDeploymentModeStore } from '../../../stores/deploymentModeStore';
import { t } from '../../../i18n';
import { errorText, type ChannelType } from '../api';

const FIELDS: Record<string, string> = {
  app_id: 'App ID / CorpID', app_secret: 'App Secret / Secret',
  agent_id: 'AgentId', token: 'Token', aes_key: 'EncodingAESKey',
};

export function ChannelBindingForm({ type, onBound, onCancel }: {
  type: ChannelType; onBound: (id: string) => Promise<void>; onCancel: () => void;
}) {
  const [adapter, setAdapter] = useState<ChannelAdapterInfo>();
  const [loadError, setLoadError] = useState('');
  const [error, setError] = useState('');
  const [creating, setCreating] = useState(false);
  const [createdId, setCreatedId] = useState<string>();
  const [webhook, setWebhook] = useState('');
  const [name, setName] = useState('');
  const [creds, setCreds] = useState<Record<string, string>>({});
  const [location, setLocation] = useState<'cloud' | 'local'>('cloud');
  const [qr, setQr] = useState(INITIAL_WEIXIN_BINDING);
  const { provisionMode, localReady } = useDeploymentModeStore();
  const mounted = useRef(false);
  const onBoundRef = useRef(onBound);
  useEffect(() => { onBoundRef.current = onBound; }, [onBound]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const flow = useMemo(() => createWeixinBindingFlow({
    start: async () => {
      const local = location === 'local' ? await prepareChannelLocalBinding() : undefined;
      return startWeixinBind(undefined, local?.binding_id);
    },
    poll: getWeixinBindStatus,
    onChange: setQr,
    onConfirmed: async id => {
      setCreatedId(id);
      try { await onBoundRef.current(id); }
      catch (exc) { if (mounted.current) setError(errorText(exc)); }
    },
  }), [location]);
  useEffect(() => () => flow.dispose(), [flow]);

  const loadAdapter = useCallback(async () => {
    setLoadError('');
    try {
      const item = (await listChannelAdapters()).find(item => item.channel_type === type);
      if (!item) throw new Error(t('渠道暂不可用'));
      if (mounted.current) setAdapter(item);
    } catch (exc) { if (mounted.current) setLoadError(errorText(exc)); }
  }, [type]);
  useEffect(() => { void loadAdapter(); }, [loadAdapter]);

  async function bind() {
    if (!createdId && (!creds.app_id?.trim() || !creds.app_secret?.trim())) {
      setError(t('请填写 App ID 和 App Secret'));
      return;
    }
    setCreating(true); setError('');
    try {
      let id = createdId;
      if (!id) {
        const local = location === 'local' ? await prepareChannelLocalBinding() : undefined;
        const extra = Object.fromEntries(Object.entries(creds).filter(([key, value]) => key !== 'app_id' && key !== 'app_secret' && value.trim()).map(([key, value]) => [key, value.trim()]));
        const bot = await createChannelBot({
          channel_type: type, app_id: creds.app_id.trim(), app_secret: creds.app_secret.trim(),
          display_name: name.trim() || undefined, execution_location: location,
          local_binding_id: local?.binding_id, transport: adapter?.supports_long_conn ? 'long_conn' : 'webhook', extra,
        });
        id = bot.channel_id;
        if (!mounted.current) return;
        setCreatedId(id); setCreds({});
        if (bot.webhook_path) { setWebhook(bot.webhook_path); return; }
      }
      await onBoundRef.current(id);
    } catch (exc) { if (mounted.current) setError(errorText(exc)); }
    finally { if (mounted.current) setCreating(false); }
  }

  const qrBusy = qr.phase === 'loading' || qr.phase === 'waiting' || qr.phase === 'scanned';
  return <div className="pa-channel-binding">
    {loadError ? <Alert type="error" title={loadError} action={<Button size="small" onClick={() => void loadAdapter()}>{t('重试')}</Button>} /> : !adapter ? <Spin size="small" /> : <>
      {provisionMode === 'dual' && !createdId && <Radio.Group value={location} disabled={creating || qrBusy} onChange={event => { flow.close(); setLocation(event.target.value); }}
        options={[{ value: 'cloud', label: t('云端') }, { value: 'local', label: t('本机'), disabled: !localReady }]} />}
      {webhook && <Alert type="info" title={t('请将回调地址填回渠道后台：{path}', { path: webhook })} />}
      {createdId ? <Button loading={creating} onClick={() => void bind()}>{webhook && !error ? t('绑定') : t('重试')}</Button> : adapter.bind_mode === 'qr' ? <>
        {qr.phase === 'loading' && <Spin size="small" />}
        {(qr.phase === 'waiting' || qr.phase === 'scanned') && <img src={'data:image/png;base64,' + qr.image} alt={t('微信扫码绑定')} width={180} height={180} />}
        {qr.tip && <div role={qr.phase === 'error' || qr.phase === 'expired' ? 'alert' : 'status'}>{qr.tip}</div>}
        {!qrBusy && <Button onClick={() => void flow.start()}>{qr.phase === 'idle' ? t('扫码绑定') : t('重试')}</Button>}
      </> : <>
        <Input aria-label={t('机器人名称（可选）')} placeholder={t('机器人名称（可选）')} value={name} disabled={creating} onChange={event => setName(event.target.value)} />
        {adapter.credential_fields.map(field => {
          const Component = field === 'app_secret' || field === 'aes_key' ? Input.Password : Input;
          return <Component key={field} aria-label={FIELDS[field] || field} placeholder={FIELDS[field] || field} value={creds[field] || ''} disabled={creating}
            onChange={event => setCreds(current => ({ ...current, [field]: event.target.value }))} />;
        })}
        <Button type="primary" loading={creating} onClick={() => void bind()}>{t('绑定')}</Button>
      </>}
    </>}
    {error && <Alert type="error" title={error} />}
    <Button type="text" disabled={creating} onClick={onCancel}>{t('取消')}</Button>
  </div>;
}
