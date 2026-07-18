import { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Popconfirm, message } from 'antd';
import {
  CheckCircleFilled, DisconnectOutlined, LinkOutlined, LoadingOutlined, ReloadOutlined,
} from '@ant-design/icons';
import {
  getLarkStatus,
  startLarkLogin,
  pollLarkLogin,
  disconnectLark,
  type LarkStatus,
} from '../../api';
import { t } from '../../i18n';

const STATUS_META: Record<LarkStatus['status'], { cls: string; label: string }> = {
  connected: { cls: 'connected', label: t('已连接') },
  pending: { cls: 'pending', label: t('授权中') },
  error: { cls: 'error', label: t('连接异常') },
  disconnected: { cls: 'disconnected', label: t('未连接') },
};

/**
 * Feishu account connection panel (feishu-cli plugin / lark-cli). Scan-code device-flow OAuth:
 * click "Connect" → backend initiates lark-cli auth login --no-wait → presents the auth QR code + user_code
 * → user scans and confirms with the Feishu App → frontend polls until connected. Credentials are persisted in the backend per-user volume and survive across sessions.
 */
export function LarkConnect() {
  const [status, setStatus] = useState<LarkStatus | null>(null);
  const [loading, setLoading] = useState(false);     // initial/manual refresh
  const [connecting, setConnecting] = useState(false); // initiating login
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = useCallback(() => {
    if (pollTimer.current) {
      clearInterval(pollTimer.current);
      pollTimer.current = null;
    }
  }, []);

  // probe=true does a real API liveness check (backend auth status --verify, which can detect credentials already invalidated by server-side rotation).
  // silent=true is used for background periodic liveness checks: does not toggle loading or pop error messages, avoiding button flicker/disturbance.
  const refresh = useCallback(async (probe = false, silent = false) => {
    if (!silent) setLoading(true);
    try {
      setStatus(await getLarkStatus(probe));
    } catch {
      if (!silent) message.error(t('获取飞书连接状态失败'));
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  // Do a real liveness check once when the panel opens: the local DB may still say "connected" while the credentials are actually invalid.
  useEffect(() => {
    refresh(true);
    return () => stopPolling();
  }, [refresh, stopPolling]);

  // When connected, do a silent liveness check every 60s while the panel is visible, to promptly reflect login invalidation (rotated/expired).
  useEffect(() => {
    if (status?.status !== 'connected') return undefined;
    const timer = setInterval(() => { void refresh(true, true); }, 60000);
    return () => clearInterval(timer);
  }, [status?.status, refresh]);

  // When pending, start polling until connected / error
  useEffect(() => {
    if (status?.status === 'pending' && !pollTimer.current) {
      pollTimer.current = setInterval(async () => {
        try {
          const s = await pollLarkLogin();
          setStatus(s);
          if (s.status === 'connected') {
            stopPolling();
            message.success(t('飞书账号已连接'));
          }
        } catch {
          /* Ignore a single poll failure, retry on the next tick */
        }
      }, 3000);
    }
    if (status && status.status !== 'pending') stopPolling();
    return undefined;
  }, [status, stopPolling]);

  const onConnect = async () => {
    setConnecting(true);
    try {
      const s = await startLarkLogin();
      setStatus(s);
      if (s.status === 'error' && s.last_error) {
        message.warning(s.last_error);
      } else if (s.status === 'pending' && !s.verification_url && !s.qr_data_uri) {
        message.warning(s.last_error || t('未能获取授权二维码，请重试'));
      }
    } catch {
      message.error(t('发起飞书登录失败'));
    } finally {
      setConnecting(false);
    }
  };

  const onDisconnect = async () => {
    stopPolling();
    try {
      setStatus(await disconnectLark());
      message.success(t('已断开飞书连接'));
    } catch {
      message.error(t('断开失败'));
    }
  };

  const meta = STATUS_META[status?.status || 'disconnected'];
  const connected = status?.status === 'connected';
  const pending = status?.status === 'pending';

  return (
    <div className="jx-conn jx-conn--lark">
      <div className="jx-conn-head">
        <div className="jx-conn-logo jx-conn-logo--lark">
          <svg viewBox="0 0 1024 1024" width="22" height="22" aria-hidden>
            <path fill="currentColor" d="M512 96C282.6 96 96 282.6 96 512s186.6 416 416 416 416-186.6 416-416S741.4 96 512 96zm205 287c-44 96-112 178-198 238 26 18 58 29 93 29 21 0 41-4 60-11l-19 60c-15 4-31 6-47 6-62 0-118-25-159-66-71 35-151 55-235 56l16-58c63-2 124-17 178-43-32-39-55-86-66-138l60-12c9 42 28 80 55 110 70-46 127-112 165-191l-201 1 14-50 257-1c8 0 14 9 11 16z"/>
          </svg>
        </div>
        <div className="jx-conn-headText">
          <div className="jx-conn-title">{t('飞书工作台')}</div>
          <div className="jx-conn-desc">
            {t('连接你的飞书账号后，智能体可在「飞书」技能里以你的身份操作消息、云文档、多维表格、日历、邮箱、任务、知识库、会议等。凭据安全保存在服务端，仅你本人可用。')}
          </div>
        </div>
      </div>

      <div className="jx-conn-statusBar">
        <div className="jx-conn-statusLeft">
          <span className={`jx-conn-badge jx-conn-badge--${meta.cls}`}>
            {connected ? <CheckCircleFilled />
              : pending ? <LoadingOutlined />
                : <span className="jx-conn-dot" />}
            {meta.label}
          </span>
          {connected && status?.lark_name && (
            <span className="jx-conn-account">{status.lark_name}</span>
          )}
        </div>
        <div className="jx-conn-actions">
          {connected ? (
            <Popconfirm title={t('确认断开飞书连接？将清除服务端保存的凭据。')} onConfirm={onDisconnect} okText={t('断开')} cancelText={t('取消')}>
              <Button danger icon={<DisconnectOutlined />}>{t('断开连接')}</Button>
            </Popconfirm>
          ) : (
            <Button type="primary" icon={<LinkOutlined />} loading={connecting} onClick={onConnect}>
              {pending ? t('重新发起') : t('连接飞书账号')}
            </Button>
          )}
          <Button icon={<ReloadOutlined />} onClick={() => refresh(true)} loading={loading}>{t('刷新状态')}</Button>
        </div>
      </div>

      {pending && (
        <div className="jx-conn-pending">
          {(status?.qr_data_uri || status?.verification_url) ? (
            <>
              {status?.qr_data_uri && (
                <div className="jx-conn-qr">
                  <img src={status.qr_data_uri} alt={t('飞书授权二维码')} />
                  <div className="jx-conn-qrTip">{t('用飞书扫一扫绑定')}</div>
                </div>
              )}
              <div className="jx-conn-pendingMain">
                <div className="jx-conn-pendingTitle">{t('用飞书 App 扫描左侧二维码，确认授权即可绑定。')}</div>
                {(status?.verification_url_complete || status?.verification_url) && (
                  <a className="jx-conn-link" href={(status?.verification_url_complete || status?.verification_url) as string} target="_blank" rel="noreferrer">
                    <LinkOutlined /> {t('或点此在浏览器中打开授权页')}
                  </a>
                )}
                {status?.user_code && (
                  <div className="jx-conn-code">{t('授权码')}：<code>{status.user_code}</code></div>
                )}
                <div className="jx-conn-hint">{t('授权完成后将自动连接，无需手动刷新。')}</div>
              </div>
            </>
          ) : (
            <div className="jx-conn-note jx-conn-note--warning">{status?.last_error || t('正在获取授权二维码…')}</div>
          )}
        </div>
      )}

      {status?.status === 'error' && status.last_error && (
        <div className="jx-conn-note jx-conn-note--error">{status.last_error}</div>
      )}
      {status?.status === 'disconnected' && status.last_error && (
        <div className="jx-conn-note jx-conn-note--warning">{status.last_error}</div>
      )}
    </div>
  );
}
