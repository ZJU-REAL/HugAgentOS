import { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Popconfirm, message } from 'antd';
import {
  CheckCircleFilled, DisconnectOutlined, LinkOutlined, LoadingOutlined, ReloadOutlined,
} from '@ant-design/icons';
import {
  getDingTalkStatus,
  startDingTalkLogin,
  pollDingTalkLogin,
  disconnectDingTalk,
  type DingTalkStatus,
} from '../../api';
import { t } from '../../i18n';

const STATUS_META: Record<DingTalkStatus['status'], { cls: string; label: string }> = {
  connected: { cls: 'connected', label: t('已连接') },
  pending: { cls: 'pending', label: t('授权中') },
  error: { cls: 'error', label: t('连接异常') },
  disconnected: { cls: 'disconnected', label: t('未连接') },
};

/**
 * DingTalk account connection panel (dingtalk skill / dws CLI). Device-flow OAuth:
 * click "Connect" → the backend runs dws auth login --device in the user sandbox → shows the verification URL + user_code
 * → the user approves on DingTalk → the frontend polls until connected. Credentials are persisted in the backend per-user volume and survive across sessions.
 */
export function DingTalkConnect() {
  const [status, setStatus] = useState<DingTalkStatus | null>(null);
  const [loading, setLoading] = useState(false);     // Initial/manual refresh
  const [connecting, setConnecting] = useState(false); // Initiating login
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = useCallback(() => {
    if (pollTimer.current) {
      clearInterval(pollTimer.current);
      pollTimer.current = null;
    }
  }, []);

  // probe=true does a real API liveness probe (backend verify_and_refresh, which can detect credentials already invalidated by server-side rotation).
  // silent=true is used for background periodic probing: it does not toggle loading or pop error messages, to avoid button flicker/interruption.
  const refresh = useCallback(async (probe = false, silent = false) => {
    if (!silent) setLoading(true);
    try {
      setStatus(await getDingTalkStatus(probe));
    } catch {
      if (!silent) message.error(t('获取钉钉连接状态失败'));
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  // Do a real liveness probe once when the panel opens: the local DB may still say "connected" while the credentials are actually invalid,
  // and without probing the user would keep seeing a fake connected state.
  useEffect(() => {
    refresh(true);
    return () => stopPolling();
  }, [refresh, stopPolling]);

  // When connected, silently probe every 60s while the panel is visible, to promptly reflect login invalidation (rotated/expired).
  // Only poll when connected (pending has its own 3s poll; other states need no probing); stops on unmount.
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
          const s = await pollDingTalkLogin();
          setStatus(s);
          if (s.status === 'connected') {
            stopPolling();
            message.success(t('钉钉账号已连接'));
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
      const s = await startDingTalkLogin();
      setStatus(s);
      if (s.status === 'pending' && !s.verification_url) {
        message.warning(s.last_error || t('未能获取授权链接，请重试'));
      }
    } catch {
      message.error(t('发起钉钉登录失败'));
    } finally {
      setConnecting(false);
    }
  };

  const onDisconnect = async () => {
    stopPolling();
    try {
      setStatus(await disconnectDingTalk());
      message.success(t('已断开钉钉连接'));
    } catch {
      message.error(t('断开失败'));
    }
  };

  const meta = STATUS_META[status?.status || 'disconnected'];
  const connected = status?.status === 'connected';
  const pending = status?.status === 'pending';

  return (
    <div className="jx-conn jx-conn--dingtalk">
      <div className="jx-conn-head">
        <div className="jx-conn-logo jx-conn-logo--dingtalk">
          <svg viewBox="0 0 1024 1024" width="22" height="22" aria-hidden>
            <path fill="currentColor" d="M512 96C282.6 96 96 282.6 96 512s186.6 416 416 416 416-186.6 416-416S741.4 96 512 96zm236 348c-12 47-50 116-50 116l1 1c-15 25-39 60-72 96l16-127-94 19c0-3 53-150 53-150L394 432s213-83 232-90c5-2 11-1 14 1 6 4 5 10 3 18-2 6-37 96-37 96l59-13c4-1 8 0 11 3 4 4 4 10 2 15-7 16-26 56-43 90l1 1c20-4 35-7 35-7s27-6 24 8z"/>
          </svg>
        </div>
        <div className="jx-conn-headText">
          <div className="jx-conn-title">{t('钉钉工作台')}</div>
          <div className="jx-conn-desc">
            {t('连接你的钉钉账号后，智能体可在「钉钉工作台」技能里以你的身份操作通讯录、日历、待办、审批、钉钉文档、群聊等。凭据安全保存在服务端，仅你本人可用。')}
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
          {connected && status?.dingtalk_name && (
            <span className="jx-conn-account">{status.dingtalk_name}{status.corp_id ? ` · ${status.corp_id}` : ''}</span>
          )}
        </div>
        <div className="jx-conn-actions">
          {connected ? (
            <Popconfirm title={t('确认断开钉钉连接？将清除服务端保存的凭据。')} onConfirm={onDisconnect} okText={t('断开')} cancelText={t('取消')}>
              <Button danger icon={<DisconnectOutlined />}>{t('断开连接')}</Button>
            </Popconfirm>
          ) : (
            <Button type="primary" icon={<LinkOutlined />} loading={connecting} onClick={onConnect}>
              {pending ? t('重新发起') : t('连接钉钉账号')}
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
                  <img src={status.qr_data_uri} alt={t('钉钉授权二维码')} />
                  <div className="jx-conn-qrTip">{t('用钉钉扫一扫绑定')}</div>
                </div>
              )}
              <div className="jx-conn-pendingMain">
                <div className="jx-conn-pendingTitle">{t('用钉钉 App 扫描左侧二维码，确认授权即可绑定。')}</div>
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
      {/* After login invalidation (server-side rotation/expiry) it falls to disconnected + an invalidation note: prompt the user to reconnect */}
      {status?.status === 'disconnected' && status.last_error && (
        <div className="jx-conn-note jx-conn-note--warning">{status.last_error}</div>
      )}
    </div>
  );
}
