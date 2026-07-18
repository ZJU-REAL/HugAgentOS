import { useCallback, useEffect, useState } from 'react';
import { Button, Collapse, Input, Popconfirm, Select, message } from 'antd';
import {
  CheckCircleFilled, DisconnectOutlined, LinkOutlined, ReloadOutlined,
} from '@ant-design/icons';
import {
  getEmailStatus,
  connectEmail,
  disconnectEmail,
  type EmailStatus,
  type EmailServerOverrides,
} from '../../api';
import { t } from '../../i18n';

const STATUS_META: Record<EmailStatus['status'], { cls: string; label: string }> = {
  connected: { cls: 'connected', label: t('已连接') },
  error: { cls: 'error', label: t('连接异常') },
  disconnected: { cls: 'disconnected', label: t('未连接') },
};

const SECURITY_OPTS = [
  { value: 'tls', label: 'TLS / SSL' },
  { value: 'starttls', label: 'STARTTLS' },
  { value: 'none', label: 'None' },
];

/**
 * Email account connection panel (email plugin / himalaya). Unlike DingTalk/Feishu: no device flow /
 * no QR code / no polling -- binds synchronously with an IMAP/SMTP app password: fill the form -> POST /connect
 * -> backend writes config.toml and does a real IMAP connect to validate -> connected / error. Credentials
 * are persisted in a backend per-user volume and survive across sessions.
 */
export function EmailConnect() {
  const [status, setStatus] = useState<EmailStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [showForm, setShowForm] = useState(false);

  // form fields
  const [email, setEmail] = useState('');
  const [secret, setSecret] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [ov, setOv] = useState<EmailServerOverrides>({});

  const refresh = useCallback(async (probe = false, silent = false) => {
    if (!silent) setLoading(true);
    try {
      setStatus(await getEmailStatus(probe));
    } catch {
      if (!silent) message.error(t('获取邮箱连接状态失败'));
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(true); }, [refresh]);

  const onConnect = async () => {
    if (!email.trim() || !secret) {
      message.warning(t('请填写邮箱地址和授权码'));
      return;
    }
    setSubmitting(true);
    try {
      const overrides = Object.fromEntries(
        Object.entries(ov).filter(([, v]) => v !== undefined && v !== '' && v !== null),
      ) as EmailServerOverrides;
      const s = await connectEmail({
        email_address: email.trim(),
        secret,
        display_name: displayName.trim() || undefined,
        server_overrides: Object.keys(overrides).length ? overrides : undefined,
      });
      setStatus(s);
      if (s.status === 'connected') {
        message.success(t('邮箱已连接'));
        setSecret('');
        setShowForm(false);
      } else if (s.last_error) {
        message.warning(s.last_error);
      }
    } catch {
      message.error(t('绑定邮箱失败'));
    } finally {
      setSubmitting(false);
    }
  };

  const onDisconnect = async () => {
    try {
      setStatus(await disconnectEmail());
      message.success(t('已断开邮箱连接'));
    } catch {
      message.error(t('断开失败'));
    }
  };

  const meta = STATUS_META[status?.status || 'disconnected'];
  const connected = status?.status === 'connected';

  const form = (
    <div className="jx-conn-form">
      <label className="jx-conn-field">
        <span className="jx-conn-fieldLabel">{t('邮箱地址')}</span>
        <Input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" autoComplete="off" />
      </label>
      <label className="jx-conn-field">
        <span className="jx-conn-fieldLabel">{t('授权码 / 密码')}</span>
        <Input.Password value={secret} onChange={(e) => setSecret(e.target.value)} autoComplete="new-password" />
      </label>
      <div className="jx-conn-hint">
        {t('请使用邮箱的授权码（app password）而非登录密码：Gmail 用 App Password；QQ / 网易 / 腾讯企业邮在邮箱设置开启 IMAP/SMTP 后获取授权码。')}
      </div>
      <label className="jx-conn-field">
        <span className="jx-conn-fieldLabel">{t('显示名（可选）')}</span>
        <Input value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder={email || 'Name'} />
      </label>

      <Collapse
        ghost
        size="small"
        items={[{
          key: 'adv',
          label: t('高级设置（手动填写服务器）'),
          children: (
            <div className="jx-conn-advGrid">
              <div className="jx-conn-fieldLabel">{t('IMAP 接收服务器')}</div>
              <div className="jx-conn-srvRow">
                <Input placeholder="imap.example.com" value={ov.imap_host ?? ''}
                  onChange={(e) => setOv({ ...ov, imap_host: e.target.value })} />
                <Input style={{ width: 96 }} placeholder={t('端口')} value={ov.imap_port ?? ''}
                  onChange={(e) => setOv({ ...ov, imap_port: e.target.value ? Number(e.target.value) : undefined })} />
                <Select style={{ width: 130 }} placeholder={t('加密')} options={SECURITY_OPTS}
                  value={ov.imap_security} onChange={(v) => setOv({ ...ov, imap_security: v })} />
              </div>
              <div className="jx-conn-fieldLabel">{t('SMTP 发送服务器')}</div>
              <div className="jx-conn-srvRow">
                <Input placeholder="smtp.example.com" value={ov.smtp_host ?? ''}
                  onChange={(e) => setOv({ ...ov, smtp_host: e.target.value })} />
                <Input style={{ width: 96 }} placeholder={t('端口')} value={ov.smtp_port ?? ''}
                  onChange={(e) => setOv({ ...ov, smtp_port: e.target.value ? Number(e.target.value) : undefined })} />
                <Select style={{ width: 130 }} placeholder={t('加密')} options={SECURITY_OPTS}
                  value={ov.smtp_security} onChange={(v) => setOv({ ...ov, smtp_security: v })} />
              </div>
              <div className="jx-conn-hint">
                {t('常见邮箱（Gmail / Outlook / QQ / 163 / 企业邮等）会自动识别服务器，通常只填邮箱地址和授权码即可。')}
              </div>
            </div>
          ),
        }]}
      />

      <div className="jx-conn-actions">
        <Button type="primary" icon={<LinkOutlined />} loading={submitting} onClick={onConnect}>
          {t('绑定邮箱')}
        </Button>
      </div>
    </div>
  );

  return (
    <div className="jx-conn jx-conn--email">
      <div className="jx-conn-head">
        <div className="jx-conn-logo jx-conn-logo--email">
          <svg viewBox="0 0 1024 1024" width="22" height="22" aria-hidden>
            <path fill="currentColor" d="M192 224h640a64 64 0 0 1 64 64v448a64 64 0 0 1-64 64H192a64 64 0 0 1-64-64V288a64 64 0 0 1 64-64zm20 96 300 232 300-232H212zm-20 80v336h640V400L512 632 192 400z"/>
          </svg>
        </div>
        <div className="jx-conn-headText">
          <div className="jx-conn-title">{t('电子邮箱')}</div>
          <div className="jx-conn-desc">
            {t('绑定你的邮箱账号后，智能体可在「电子邮箱」技能里以你的身份收发和管理邮件（含附件）。凭据安全保存在服务端，仅你本人可用。')}
          </div>
        </div>
      </div>

      <div className="jx-conn-statusBar">
        <div className="jx-conn-statusLeft">
          <span className={`jx-conn-badge jx-conn-badge--${meta.cls}`}>
            {connected ? <CheckCircleFilled /> : <span className="jx-conn-dot" />}
            {meta.label}
          </span>
          {connected && status?.email_address && (
            <span className="jx-conn-account">{status.email_address}</span>
          )}
        </div>
        <div className="jx-conn-actions">
          {connected ? (
            <>
              <Button icon={<LinkOutlined />} onClick={() => { setShowForm((v) => !v); setEmail(status?.email_address || ''); }}>
                {t('重新绑定')}
              </Button>
              <Popconfirm title={t('确认断开邮箱连接？将清除服务端保存的凭据。')} onConfirm={onDisconnect} okText={t('断开')} cancelText={t('取消')}>
                <Button danger icon={<DisconnectOutlined />}>{t('断开连接')}</Button>
              </Popconfirm>
            </>
          ) : (
            <Button type="primary" icon={<LinkOutlined />} onClick={() => setShowForm((v) => !v)}>
              {t('绑定邮箱')}
            </Button>
          )}
          <Button icon={<ReloadOutlined />} onClick={() => refresh(true)} loading={loading}>{t('刷新状态')}</Button>
        </div>
      </div>

      {(showForm || (!connected && status?.status !== 'error')) && form}

      {status?.status === 'error' && status.last_error && (
        <div className="jx-conn-note jx-conn-note--error">{status.last_error}</div>
      )}
    </div>
  );
}
