import { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Input, Modal, Popconfirm, Select, Spin, Switch, Tag, message } from 'antd';
import {
  CheckCircleFilled, DeleteOutlined, PlusOutlined, ReloadOutlined, RobotOutlined,
  ScanOutlined, ThunderboltOutlined,
} from '@ant-design/icons';
import {
  listChannelAdapters, listChannelBots, createChannelBot, updateChannelBot, deleteChannelBot,
  testChannelBot, startWeixinBind, getWeixinBindStatus,
  type ChannelAdapterInfo, type ChannelBot, type CreateChannelBotPayload,
} from '../../api';
import { t } from '../../i18n';

const STATUS_META: Record<ChannelBot['status'], { color: string; label: string }> = {
  connected: { color: 'success', label: t('已连接') },
  pending: { color: 'processing', label: t('连接中') },
  error: { color: 'error', label: t('异常') },
  disconnected: { color: 'default', label: t('已停用') },
};

const CHANNEL_LABELS: Record<string, string> = {
  lark: t('飞书'), dingtalk: t('钉钉'), wecom: t('企业微信'), weixin: t('微信（扫码）'),
};

// Display labels/placeholders for each credential field (app_id/app_secret are the two core columns, the rest go into extra).
const FIELD_META: Record<string, { label: string; placeholder: string; password?: boolean }> = {
  app_id: { label: 'App ID', placeholder: t('App ID / CorpID') },
  app_secret: { label: 'App Secret', placeholder: t('App Secret / Secret'), password: true },
  agent_id: { label: 'AgentId', placeholder: t('应用 AgentId') },
  token: { label: 'Token', placeholder: t('回调 Token') },
  aes_key: { label: 'EncodingAESKey', placeholder: t('回调 EncodingAESKey'), password: true },
};

interface ChannelBotsPanelProps {
  /** Bind to a specific sub-agent (used by the sub-agent page): when provided, only list/create bots for that sub-agent;
   *  when omitted (used by the "My Bots" setting), only list/create bots for the main agent (owner default capabilities). */
  agentId?: string;
  agentName?: string;
}

/**
 * Channel bot binding panel (inbound channel bots, owner service-account model).
 * Multiple channels: Lark / DingTalk (credential form), WeCom (credential form + webhook callback), WeChat (QR-code binding of a personal account).
 * Two usages:
 *  - Without agentId (the "My Bots" setting): the bot runs as yourself + all default capabilities (main agent).
 *  - With agentId (the sub-agent page): messages the bot receives are always answered by that sub-agent, using its own bound capabilities.
 */
export function ChannelBotsPanel({ agentId, agentName }: ChannelBotsPanelProps = {}) {
  const scopedToAgent = !!agentId;
  const [adapters, setAdapters] = useState<ChannelAdapterInfo[]>([]);
  const [bots, setBots] = useState<ChannelBot[]>([]);
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [showForm, setShowForm] = useState(false);

  // Form
  const [channelType, setChannelType] = useState('lark');
  const [displayName, setDisplayName] = useState('');
  const [creds, setCreds] = useState<Record<string, string>>({});
  const [transport, setTransport] = useState<'long_conn' | 'webhook'>('long_conn');
  const [encryptKey, setEncryptKey] = useState('');
  const [verificationToken, setVerificationToken] = useState('');

  // WeChat QR code
  const [qrOpen, setQrOpen] = useState(false);
  const [qrImg, setQrImg] = useState('');
  const [qrTip, setQrTip] = useState('');
  const qrTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  const adapter = adapters.find((a) => a.channel_type === channelType);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setBots(await listChannelBots(agentId ? { agentId } : { mainOnly: true }));
    } catch {
      message.error(t('加载机器人列表失败'));
    } finally {
      setLoading(false);
    }
  }, [agentId]);

  useEffect(() => {
    void refresh();
    void listChannelAdapters().then(setAdapters).catch(() => undefined);
  }, [refresh]);

  // Reset transport when switching channels: only Lark can choose long connection/webhook; others are fixed by channel capability.
  useEffect(() => {
    if (!adapter) return;
    if (channelType === 'lark') setTransport('long_conn');
    else setTransport(adapter.supports_long_conn ? 'long_conn' : 'webhook');
  }, [channelType, adapter]);

  const resetForm = () => {
    setDisplayName(''); setCreds({}); setTransport('long_conn');
    setEncryptKey(''); setVerificationToken('');
  };

  const onCreate = async () => {
    const fields = adapter?.credential_fields ?? ['app_id', 'app_secret'];
    if (!creds.app_id?.trim() || !creds.app_secret?.trim()) {
      message.warning(t('请填写 App ID 和 App Secret'));
      return;
    }
    setCreating(true);
    try {
      const extra: Record<string, string> = {};
      for (const f of fields) {
        if (f === 'app_id' || f === 'app_secret') continue;
        if (creds[f]?.trim()) extra[f] = creds[f].trim();
      }
      if (channelType === 'lark' && transport === 'webhook') {
        if (encryptKey.trim()) extra.encrypt_key = encryptKey.trim();
        if (verificationToken.trim()) extra.verification_token = verificationToken.trim();
      }
      const payload: CreateChannelBotPayload = {
        channel_type: channelType,
        app_id: creds.app_id.trim(),
        app_secret: creds.app_secret.trim(),
        display_name: displayName.trim() || undefined,
        transport,
        extra: Object.keys(extra).length ? extra : undefined,
        agent_id: agentId || undefined,
      };
      const bot = await createChannelBot(payload);
      message.success(t('机器人已绑定'));
      if (bot.webhook_path) {
        message.info(t('请将回调地址填回渠道后台：{path}', { path: bot.webhook_path }));
      }
      resetForm();
      setShowForm(false);
      await refresh();
    } catch (e) {
      message.error((e as Error)?.message || t('绑定失败'));
    } finally {
      setCreating(false);
    }
  };

  // ── WeChat QR-code binding ───────────────────────────────────────────
  const stopQrPoll = () => {
    if (qrTimer.current) { clearInterval(qrTimer.current); qrTimer.current = null; }
  };

  const onWeixinScan = async () => {
    setQrImg(''); setQrTip(t('正在获取二维码…')); setQrOpen(true);
    try {
      const { bind_id, qrcode_img } = await startWeixinBind(agentId);
      setQrImg(qrcode_img);
      setQrTip(t('请用微信扫描二维码并确认登录'));
      let elapsed = 0;
      stopQrPoll();
      qrTimer.current = setInterval(async () => {
        elapsed += 2;
        if (elapsed > 300) { stopQrPoll(); setQrTip(t('二维码已过期，请重试')); return; }
        try {
          const st = await getWeixinBindStatus(bind_id);
          if (st.status === 'confirmed') {
            stopQrPoll();
            message.success(t('微信已绑定'));
            setQrOpen(false);
            await refresh();
          } else if (st.status === 'scanned') {
            setQrTip(t('已扫描，请在手机上确认'));
          }
        } catch {
          stopQrPoll();
          setQrTip(t('绑定失败，请重试'));
        }
      }, 2000);
    } catch (e) {
      setQrTip((e as Error)?.message || t('获取二维码失败'));
    }
  };

  useEffect(() => () => stopQrPoll(), []);

  const onToggle = async (bot: ChannelBot, enabled: boolean) => {
    try { await updateChannelBot(bot.channel_id, { enabled }); await refresh(); }
    catch (e) { message.error((e as Error)?.message || t('操作失败')); }
  };
  const onTest = async (bot: ChannelBot) => {
    try { await testChannelBot(bot.channel_id); message.success(t('凭据有效')); }
    catch (e) { message.error((e as Error)?.message || t('测试失败')); }
    finally { await refresh(); }  // Refresh on both success/failure so the connected/error status written back by the backend shows promptly
  };
  const onDelete = async (bot: ChannelBot) => {
    try { await deleteChannelBot(bot.channel_id); message.success(t('已删除')); await refresh(); }
    catch (e) { message.error((e as Error)?.message || t('删除失败')); }
  };

  const channelOptions = adapters
    .filter((a) => CHANNEL_LABELS[a.channel_type])
    .map((a) => ({ value: a.channel_type, label: CHANNEL_LABELS[a.channel_type] }));
  const coreFields = (adapter?.credential_fields ?? ['app_id', 'app_secret']).filter((f) => FIELD_META[f]);
  const isQr = adapter?.bind_mode === 'qr';

  return (
    <div className="jx-conn">
      <div className="jx-conn-head">
        <div className="jx-conn-logo jx-conn-logo--lark"><RobotOutlined /></div>
        <div>
          <div className="jx-conn-title">{scopedToAgent ? t('渠道机器人') : t('我的机器人')}</div>
          <div className="jx-conn-desc">
            {scopedToAgent
              ? t('为子智能体「{name}」绑定渠道机器人（飞书 / 钉钉 / 企业微信 / 微信）：消息推给它，就由该子智能体用它自己绑定的能力回复。', { name: agentName || '' })
              : t('绑定你自己的渠道机器人（飞书 / 钉钉 / 企业微信 / 微信）：消息推给它就由你的智能体回复，复用你的知识库与技能。')}
          </div>
        </div>
      </div>

      <div className="jx-conn-note jx-conn-note--warning" style={{ marginTop: 12 }}>
        {scopedToAgent
          ? t('注意：机器人以你本人的权限运行——群里任何人 @ 它，都能隔着机器人用到该子智能体的能力。')
          : t('注意：机器人以你本人的权限运行——群里任何人 @ 它，都能隔着机器人用到你的知识库与技能。可在「资源范围」里收窄暴露范围。')}
      </div>

      {/* List */}
      <div style={{ marginTop: 16 }}>
        {bots.length === 0 && !loading && (
          <div className="jx-conn-desc" style={{ padding: '8px 0' }}>{t('还没有机器人，点下方「绑定机器人」创建。')}</div>
        )}
        {bots.map((bot) => {
          const meta = STATUS_META[bot.status];
          return (
            <div key={bot.channel_id} className="jx-settings-card" style={{ marginBottom: 10 }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontWeight: 500 }}>
                    {bot.display_name} <Tag color={meta.color}>{meta.label}</Tag>
                    <Tag>{CHANNEL_LABELS[bot.channel_type] || bot.channel_type}</Tag>
                    <Tag>{bot.transport === 'long_conn' ? t('长连接') : 'Webhook'}</Tag>
                  </div>
                  <div className="jx-conn-desc" style={{ fontSize: 12 }}>
                    {bot.channel_type === 'weixin' ? t('微信号') : 'App ID'}: {bot.app_id}
                  </div>
                  {bot.status === 'error' && bot.last_error && (
                    <div className="jx-conn-desc" style={{ color: '#d4380d', fontSize: 12 }}>{bot.last_error}</div>
                  )}
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
                  <Switch size="small" checked={bot.enabled} onChange={(v) => onToggle(bot, v)} />
                  <Button size="small" icon={<ThunderboltOutlined />} onClick={() => onTest(bot)}>{t('测试')}</Button>
                  <Popconfirm title={t('确认删除该机器人？将清除凭据。')} onConfirm={() => onDelete(bot)} okText={t('删除')} cancelText={t('取消')}>
                    <Button size="small" danger icon={<DeleteOutlined />} />
                  </Popconfirm>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* New form */}
      {showForm ? (
        <div className="jx-settings-card" style={{ marginTop: 12 }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span className="jx-conn-desc">{t('渠道')}</span>
              <Select
                size="small" value={channelType} style={{ width: 200 }}
                onChange={(v) => { setChannelType(v); setCreds({}); }}
                options={channelOptions.length ? channelOptions : [{ value: 'lark', label: CHANNEL_LABELS.lark }]}
              />
            </div>

            {isQr ? (
              <div className="jx-conn-desc">
                {t('微信走扫码绑定个人微信号，无需填凭据。')}
                <div style={{ marginTop: 8 }}>
                  <Button type="primary" icon={<ScanOutlined />} onClick={() => void onWeixinScan()}>{t('扫码绑定')}</Button>
                </div>
              </div>
            ) : (
              <>
                <Input placeholder={t('机器人名称（可选）')} value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
                {coreFields.map((f) => {
                  const fm = FIELD_META[f];
                  const Comp = fm.password ? Input.Password : Input;
                  return (
                    <Comp key={f} placeholder={fm.placeholder} value={creds[f] || ''}
                      onChange={(e) => setCreds((c) => ({ ...c, [f]: e.target.value }))} />
                  );
                })}
                {channelType === 'lark' && (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span className="jx-conn-desc">{t('接入方式')}</span>
                    <Select
                      size="small" value={transport} style={{ width: 220 }} onChange={(v) => setTransport(v)}
                      options={[
                        { value: 'long_conn', label: t('长连接（推荐，免公网回调）') },
                        { value: 'webhook', label: t('Webhook（需公网回调）') },
                      ]}
                    />
                  </div>
                )}
                {channelType === 'lark' && transport === 'webhook' && (
                  <>
                    <Input placeholder={t('Encrypt Key（飞书事件加密时填）')} value={encryptKey} onChange={(e) => setEncryptKey(e.target.value)} />
                    <Input placeholder={t('Verification Token（可选）')} value={verificationToken} onChange={(e) => setVerificationToken(e.target.value)} />
                  </>
                )}
                {channelType === 'wecom' && (
                  <div className="jx-conn-note" style={{ fontSize: 12 }}>
                    {t('企业微信走回调模式：绑定后把回吐的回调地址填回「企业微信后台 → 自建应用 → 接收消息」。')}
                  </div>
                )}
                <div style={{ display: 'flex', gap: 8 }}>
                  <Button type="primary" icon={<CheckCircleFilled />} loading={creating} onClick={() => void onCreate()}>{t('绑定')}</Button>
                  <Button onClick={() => { setShowForm(false); resetForm(); }}>{t('取消')}</Button>
                </div>
              </>
            )}
          </div>
        </div>
      ) : (
        <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setShowForm(true)}>{t('绑定机器人')}</Button>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void refresh()}>{t('刷新')}</Button>
        </div>
      )}

      {/* WeChat QR-code Modal */}
      <Modal
        open={qrOpen} title={t('微信扫码绑定')} footer={null}
        onCancel={() => { stopQrPoll(); setQrOpen(false); }}
        destroyOnClose maskClosable={false}
      >
        <div style={{ textAlign: 'center', padding: '12px 0' }}>
          {qrImg ? (
            <img src={`data:image/png;base64,${qrImg}`} alt="qrcode" style={{ width: 200, height: 200 }} />
          ) : (
            <Spin />
          )}
          <div className="jx-conn-desc" style={{ marginTop: 12 }}>{qrTip}</div>
        </div>
      </Modal>
    </div>
  );
}
