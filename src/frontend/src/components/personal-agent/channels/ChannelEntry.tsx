import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Modal, message } from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import { ChannelBindingForm } from './ChannelBindingForm';
import { listChannelBots, testChannelBot, type ChannelBot } from '../../../api';
import './channel-binding.css';
import { personalApi, errorText, type ChannelType, type PersonalChannel, type PersonalConversation } from '../api';
import { t } from '../../../i18n';
import { CHANNELS } from './channelMeta';
import { ChannelAllowed } from './ChannelAllowed';
import { subscribePersonalChanges } from '../shared/liveFeed';

/** The change-feed topic for the owner's channel bots (personal_agent/conversation/live.py). */
const CHANNELS_TOPIC = 'channels';

export function ChannelMark({ type, size = 20 }: { type?: ChannelType; size?: number }) {
  const meta = CHANNELS.find(item => item.type === type) || CHANNELS[0];
  return <span className={'pa-channel-mark pa-channel-mark--' + meta.tint} style={{ width: size, height: size, fontSize: Math.round(size * 0.55) }} aria-hidden="true">{meta.mark}</span>;
}

/** Why a bot's binding counts as failed; empty when it is healthy. */
function failureReasons(bot: PersonalChannel, detail: ChannelBot | undefined, failure: string | undefined): string[] {
  const reasons: string[] = [];
  if (failure || bot.status === 'error') reasons.push(failure || detail?.last_error || t('绑定失败，请重试'));
  if (!bot.enabled) reasons.push(t('已停用'));
  else if (bot.status === 'disconnected') reasons.push(t('连接已断开，请重试'));
  if (detail?.device_online === false) reasons.push(t('设备离线'));
  return reasons;
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
  const load = useCallback(async () => {
    try { setBots(await personalApi.channels()); } catch (exc) { message.error(errorText(exc)); }
  }, []);
  useEffect(() => { void load(); }, [load]);

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
  // While the dialog is open: each bot's connection is checked with its IM platform when the dialog opens and when
  // the page is shown again; in between, the change feed says when a bot or its allowed accounts changed. Nothing polls.
  useEffect(() => {
    if (!open) return;
    // One read at a time, in order: a later read would otherwise discard a platform check still in flight.
    let chain = refresh(true);
    const next = (validate: boolean) => { chain = chain.then(() => refresh(validate)); };
    const unsubscribe = subscribePersonalChanges(signal => {
      // A bot's own row carries the channels topic; allowed accounts live in the agent's settings,
      // which change without a topic.
      if (signal.type === 'ready' || (signal.type === 'changed' && (!signal.topics.length || signal.topics.includes(CHANNELS_TOPIC)))) next(false);
    });
    const shown = () => { if (document.visibilityState === 'visible') next(true); };
    document.addEventListener('visibilitychange', shown);
    return () => { unsubscribe(); revision.current += 1; document.removeEventListener('visibilitychange', shown); };
  }, [open, refresh]);
  // Hugo answers every bot bound here; nothing else to set up.
  async function bound() {
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
        <p className="pa-hint">{t('在钉钉、飞书、微信、企业微信或 Slack 里直接给 {name} 发消息。回复会发回同一个会话。', { name: agentName })}</p>
        <div className="pa-channel-grid">
          {CHANNELS.map(meta => {
            const items = bots.filter(bot => bot.channel_type === meta.type);
            const connected = items.some(bot => bot.enabled && bot.status === 'connected' && !failures[bot.channel_id] && details.find(item => item.channel_id === bot.channel_id)?.device_online !== false);
            const reasons = [...new Set(items.flatMap(bot => failureReasons(bot, details.find(item => item.channel_id === bot.channel_id), failures[bot.channel_id])))];
            const unhealthy = reasons.length > 0;
            return <section key={meta.type} className="pa-channel-card" data-channel={meta.type}>
              <header><ChannelMark type={meta.type} size={28} /><strong>{t(meta.label)}</strong>
                <span className={connected && !unhealthy && !checkError ? 'pa-channel-state pa-channel-state--on' : 'pa-channel-state'} role="status">{checkError && items.length ? t('状态待检查') : unhealthy ? t('绑定异常') : connected ? t('绑定成功') : items.length ? t('连接中') : t('未接入')}</span></header>
              {unhealthy
                // A failed binding shows only why it failed, then the rebind / retry buttons below.
                ? reasons.map(reason => <Alert key={reason} type="error" title={reason} />)
                : items.map(bot => <div key={bot.channel_id}>
                  <ChannelAllowed bot={bot} onChange={setBots} onRefresh={() => void refresh()} />
                  {onSelectChat && <div className="pa-channel-conversations">
                    {conversations.filter(item => item.channel_id === bot.channel_id).map(item =>
                      <Button key={item.chat_id} block type={selectedChatId === item.chat_id ? 'default' : 'text'}
                        aria-current={selectedChatId === item.chat_id ? 'page' : undefined}
                        onClick={() => { onSelectChat(item.chat_id); setOpen(false); setBinding(undefined); }}>
                        {t('渠道会话')}
                      </Button>)}
                  </div>}
                </div>)}
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
