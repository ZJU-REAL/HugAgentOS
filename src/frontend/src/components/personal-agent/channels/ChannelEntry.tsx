import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Modal, message } from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import { ChannelBindingForm } from './ChannelBindingForm';
import { listChannelBots, testChannelBot, type ChannelBot } from '../../../api';
import './channel-binding.css';
import { personalApi, errorText, type ChannelType, type PersonalChannel, type PersonalConversation } from '../api';
import { t } from '../../../i18n';
import { CHANNELS } from './channelMeta';

export function ChannelMark({ type, size = 20 }: { type?: ChannelType; size?: number }) {
  const meta = CHANNELS.find(item => item.type === type) || CHANNELS[0];
  return <span className={'pa-channel-mark pa-channel-mark--' + meta.tint} style={{ width: size, height: size, fontSize: Math.round(size * 0.55) }} aria-hidden="true">{meta.mark}</span>;
}

interface ChannelEntryProps {
  agentName: string;
  conversations?: PersonalConversation[];
  selectedChatId?: string;
  onSelectChat?: (id?: string) => void;
}

/** Channel connection entry and per-channel conversations. */
export function ChannelEntry({ agentName, conversations = [], selectedChatId, onSelectChat }: ChannelEntryProps) {
  const [bots, setBots] = useState<PersonalChannel[]>([]);
  const [open, setOpen] = useState(false);
  const [binding, setBinding] = useState<ChannelType>();
  const [details, setDetails] = useState<ChannelBot[]>([]);
  const [checkError, setCheckError] = useState('');
  const [failures, setFailures] = useState<Record<string, string>>({});
  const [checking, setChecking] = useState(false);
  const revision = useRef(0);
  const [busy, setBusy] = useState<string>();
  const load = useCallback(async () => {
    try { setBots(await personalApi.channels()); } catch (exc) { message.error(errorText(exc)); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  async function toggle(bot: PersonalChannel, enabled: boolean) {
    setBusy(bot.channel_id);
    try { setBots(await personalApi.routeChannel(bot.channel_id, enabled)); }
    catch (exc) { message.error(errorText(exc)); }
    finally { setBusy(undefined); }
  }
  const refresh = useCallback(async (validate = false) => {
    const current = ++revision.current;
    setChecking(true);
    try {
      let items = await listChannelBots({ mainOnly: true });
      let checked: Record<string, string> | undefined;
      if (validate) {
        const enabled = items.filter(bot => bot.enabled && bot.device_online !== false);
        const results = await Promise.allSettled(enabled.map(bot => testChannelBot(bot.channel_id)));
        checked = {};
        results.forEach((result, index) => {
          if (result.status === 'rejected') checked![enabled[index].channel_id] = errorText(result.reason);
          else if (!result.value.ok) checked![enabled[index].channel_id] = t('绑定失败，请重试');
        });
        items = await listChannelBots({ mainOnly: true });
      }
      const channels = await personalApi.channels();
      if (current !== revision.current) return;
      setDetails(items); setBots(channels);
      if (checked) setFailures(checked);
      setCheckError('');
    } catch (exc) { if (current === revision.current) setCheckError(errorText(exc)); }
    finally { if (current === revision.current) setChecking(false); }
  }, []);
  useEffect(() => {
    if (!open) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    let lastCheck = 0;
    const poll = async () => {
      const validate = Date.now() - lastCheck >= 30000;
      if (validate) lastCheck = Date.now();
      await refresh(validate);
      if (active) timer = setTimeout(() => void poll(), 5000);
    };
    void poll();
    const focus = () => { if (document.visibilityState === 'visible') lastCheck = 0; };
    document.addEventListener('visibilitychange', focus);
    return () => { active = false; clearTimeout(timer); revision.current += 1; document.removeEventListener('visibilitychange', focus); };
  }, [open, refresh]);
  async function bound(id: string) {
    await personalApi.routeChannel(id, true);
    await refresh(true);
    setBinding(undefined);
  }
  return <>
    <div className="pa-side-channels">
      <button type="button" className="pa-side-chip pa-side-chip--wide" onClick={() => setOpen(true)} aria-label={t('接入频道')}>
        <PlusOutlined /><span>{t('接入频道')}</span>
      </button>
    </div>
    <Modal open={open} width={600} footer={null} destroyOnHidden onCancel={() => { setOpen(false); setBinding(undefined); }}
      title={t('接入频道')}>
      <>
        <p className="pa-hint">{t('在钉钉、飞书、微信或企业微信里直接给 {name} 发消息。回复会发回同一个会话。', { name: agentName })}</p>
        <div className="pa-channel-grid">
          {CHANNELS.map(meta => {
            const items = bots.filter(bot => bot.channel_type === meta.type);
            const connected = items.some(bot => bot.enabled && bot.status === 'connected' && !failures[bot.channel_id] && details.find(item => item.channel_id === bot.channel_id)?.device_online !== false);
            const unhealthy = items.some(bot => bot.status === 'error' || !bot.enabled || bot.status === 'disconnected' || failures[bot.channel_id] || details.find(item => item.channel_id === bot.channel_id)?.device_online === false);
            return <section key={meta.type} className="pa-channel-card" data-channel={meta.type}>
              <header><ChannelMark type={meta.type} size={28} /><strong>{t(meta.label)}</strong>
                <span className={connected && !unhealthy && !checkError ? 'pa-channel-state pa-channel-state--on' : 'pa-channel-state'} role="status">{checkError && items.length ? t('状态待检查') : unhealthy ? t('绑定异常') : connected ? t('绑定成功') : items.length ? t('连接中') : t('未接入')}</span></header>
              {items.map(bot => {
                const detail = details.find(item => item.channel_id === bot.channel_id);
                return <div key={bot.channel_id}>
                  {!bot.enabled && <span className="pa-channel-state">{t('已停用')}</span>}
                  {detail?.device_online === false && <Alert type="warning" title={t('设备离线')} />}
                  {(bot.status === 'error' || failures[bot.channel_id]) && <Alert type="error" title={failures[bot.channel_id] || detail?.last_error || t('绑定失败，请重试')} />}
                  {bot.status === 'disconnected' && bot.enabled && <Alert type="warning" title={t('连接已断开，请重试')} />}
                  {onSelectChat && bot.personal_agent && <div className="pa-channel-conversations">
                    {conversations.filter(item => item.channel_id === bot.channel_id).map(item =>
                      <Button key={item.chat_id} block type={selectedChatId === item.chat_id ? 'default' : 'text'}
                        aria-current={selectedChatId === item.chat_id ? 'page' : undefined}
                        onClick={() => { onSelectChat(item.chat_id); setOpen(false); setBinding(undefined); }}>
                        {t('渠道会话')}
                      </Button>)}
                  </div>}
                  {(!bot.personal_agent && bot.enabled) && <Button size="small" onClick={() => void toggle(bot, true)} loading={busy === bot.channel_id}>{t('交给 {name} 处理', { name: agentName })}</Button>}
                </div>;
              })}
              {open && binding === meta.type ? <ChannelBindingForm key={meta.type} type={meta.type} onBound={bound} onCancel={() => setBinding(undefined)} /> :
                (!items.length || (unhealthy && meta.type === 'weixin')) && <Button size="small" type="dashed" icon={<PlusOutlined />} onClick={() => setBinding(meta.type)}>{items.length ? t('重新绑定') : t('绑定机器人')}</Button>}
              {unhealthy && <Button size="small" loading={checking} onClick={() => void refresh(true)}>{t('重试')}</Button>}
            </section>;
          })}
        </div>
        {checkError && <Alert type="warning" title={checkError} action={<Button size="small" loading={checking} onClick={() => void refresh(true)}>{t('重试')}</Button>} />}
      </>
    </Modal>
  </>;
}
