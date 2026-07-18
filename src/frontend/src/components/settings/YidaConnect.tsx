import { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Popconfirm, message } from 'antd';
import {
  CheckCircleFilled, DisconnectOutlined, LinkOutlined, LoadingOutlined, ReloadOutlined,
} from '@ant-design/icons';
import {
  getYidaStatus,
  startYidaLogin,
  pollYidaLogin,
  disconnectYida,
  type YidaStatus,
} from '../../api';
import { t } from '../../i18n';

const STATUS_META: Record<YidaStatus['status'], { cls: string; label: string }> = {
  connected: { cls: 'connected', label: t('已连接') },
  pending: { cls: 'pending', label: t('等待扫码') },
  corp_selection: { cls: 'pending', label: t('选择组织') },
  error: { cls: 'error', label: t('连接异常') },
  disconnected: { cls: 'disconnected', label: t('未连接') },
};

/**
 * Yida account connection panel (yida plugin / openyida CLI). QR-code login:
 * click "Connect" → backend borrows the user sandbox to run openyida login --agent-qr → emits QR code
 * → user scans with DingTalk → frontend sequential long-polling (backend agent-poll can block ~45s per call,
 * must wait for the previous one to return before starting the next; cannot stack setInterval) → for multiple
 * organizations, let the user pick an organization then re-poll with corp_id → connected.
 * Login cookie is persisted in the server-side Yida working directory, surviving across sessions; in-chat QR login writes to the same one.
 */
export function YidaConnect() {
  const [status, setStatus] = useState<YidaStatus | null>(null);
  const [loading, setLoading] = useState(false);      // initial / manual refresh
  const [connecting, setConnecting] = useState(false); // login in progress
  const pollingRef = useRef(false);   // sequential long-polling in progress
  const cancelledRef = useRef(false); // stop the loop on component unmount / disconnect

  const refresh = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      setStatus(await getYidaStatus());
    } catch {
      if (!silent) message.error(t('获取宜搭连接状态失败'));
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  useEffect(() => {
    cancelledRef.current = false;
    void refresh();
    return () => { cancelledRef.current = true; };
  }, [refresh]);

  // Sequential long-polling: each time wait for the backend to return (internally blocking in the sandbox waiting for a scan), then immediately start the next,
  // until connected / corp_selection / a stop condition.
  const pollLoop = useCallback(async (corpId?: string) => {
    if (pollingRef.current) return;
    pollingRef.current = true;
    try {
      for (;;) {
        if (cancelledRef.current) return;
        let s: YidaStatus;
        try {
          s = await pollYidaLogin(corpId);
        } catch {
          // single failure (network jitter, etc.) — retry after a short delay
          await new Promise((r) => setTimeout(r, 3000));
          continue;
        }
        if (cancelledRef.current) return;
        // if a pending response lacks the QR code (old backend version / exceptional path), keep the one currently displayed,
        // to avoid a wholesale overwrite clobbering the QR code and reverting the panel to "Fetching login QR code…".
        setStatus((prev) => (
          s.status === 'pending' && !s.qr_data_uri && prev?.qr_data_uri
            ? { ...s, qr_data_uri: prev.qr_data_uri, qr_url: prev.qr_url }
            : s
        ));
        if (s.status === 'connected') {
          message.success(t('宜搭账号已连接'));
          return;
        }
        if (s.status === 'corp_selection') return; // wait for the user to pick an organization before re-entering the loop
        if (s.status === 'disconnected' || s.status === 'error') return;
        // pending: continue the next long-polling round after a brief interval
        await new Promise((r) => setTimeout(r, 1500));
      }
    } finally {
      pollingRef.current = false;
    }
  }, []);

  const onConnect = async () => {
    setConnecting(true);
    try {
      const s = await startYidaLogin();
      setStatus(s);
      if (s.status === 'pending') {
        void pollLoop();
      } else if (s.status === 'error') {
        message.error(s.error || t('发起宜搭登录失败'));
      }
    } catch {
      message.error(t('发起宜搭登录失败'));
    } finally {
      setConnecting(false);
    }
  };

  const onSelectCorp = (corpId: string) => {
    setStatus((prev) => (prev ? { ...prev, status: 'pending' } : prev));
    void pollLoop(corpId);
  };

  const onDisconnect = async () => {
    cancelledRef.current = true;
    try {
      setStatus(await disconnectYida());
      message.success(t('已断开宜搭连接'));
    } catch {
      message.error(t('断开失败'));
    } finally {
      cancelledRef.current = false;
    }
  };

  const meta = STATUS_META[status?.status || 'disconnected'];
  const connected = status?.status === 'connected';
  const pending = status?.status === 'pending';
  const corpSelection = status?.status === 'corp_selection';

  return (
    <div className="jx-conn jx-conn--yida">
      <div className="jx-conn-head">
        <div className="jx-conn-logo jx-conn-logo--yida">
          <svg viewBox="0 0 1024 1024" width="22" height="22" aria-hidden>
            <path fill="currentColor" d="M512 64L128 256v512l384 192 384-192V256L512 64zm0 96l288 144-288 144-288-144 288-144zM224 400l256 128v288L224 688V400zm576 288l-256 128V528l256-128v288z"/>
          </svg>
        </div>
        <div className="jx-conn-headText">
          <div className="jx-conn-title">{t('宜搭低代码平台')}</div>
          <div className="jx-conn-desc">
            {t('用钉钉扫码连接宜搭账号后，智能体可在「宜搭低代码平台」技能里以你的身份创建应用、表单、流程与报表。登录态安全保存在服务端，仅你本人可用。')}
          </div>
        </div>
      </div>

      <div className="jx-conn-statusBar">
        <div className="jx-conn-statusLeft">
          <span className={`jx-conn-badge jx-conn-badge--${meta.cls}`}>
            {connected ? <CheckCircleFilled />
              : (pending || corpSelection) ? <LoadingOutlined />
                : <span className="jx-conn-dot" />}
            {meta.label}
          </span>
          {connected && status?.corp_id && (
            <span className="jx-conn-account">{status.corp_id}</span>
          )}
        </div>
        <div className="jx-conn-actions">
          {connected ? (
            <Popconfirm title={t('确认断开宜搭连接？将清除服务端保存的登录态。')} onConfirm={onDisconnect} okText={t('断开')} cancelText={t('取消')}>
              <Button danger icon={<DisconnectOutlined />}>{t('断开连接')}</Button>
            </Popconfirm>
          ) : (
            <Button type="primary" icon={<LinkOutlined />} loading={connecting} onClick={onConnect}>
              {pending || corpSelection ? t('重新发起') : t('连接宜搭账号')}
            </Button>
          )}
          <Button icon={<ReloadOutlined />} onClick={() => refresh()} loading={loading}>{t('刷新状态')}</Button>
        </div>
      </div>

      {pending && (
        <div className="jx-conn-pending">
          {status?.qr_data_uri ? (
            <>
              <div className="jx-conn-qr">
                <img src={status.qr_data_uri} alt={t('宜搭登录二维码')} />
                <div className="jx-conn-qrTip">{t('用钉钉扫一扫登录')}</div>
              </div>
              <div className="jx-conn-pendingMain">
                <div className="jx-conn-pendingTitle">{t('用钉钉 App 扫描左侧二维码，确认登录即可连接。')}</div>
                <div className="jx-conn-hint">{t('扫码完成后将自动连接，无需手动刷新。')}</div>
              </div>
            </>
          ) : (
            <div className="jx-conn-note jx-conn-note--warning">{status?.error || t('正在获取登录二维码…')}</div>
          )}
        </div>
      )}

      {corpSelection && (
        <div className="jx-conn-pending">
          <div className="jx-conn-pendingMain">
            <div className="jx-conn-pendingTitle">{t('你的账号属于多个组织，请选择要登录的宜搭组织：')}</div>
            <div className="jx-conn-orgList">
              {(status?.organizations || []).map((org) => (
                <Button key={org.corp_id} onClick={() => onSelectCorp(org.corp_id)}>
                  {org.corp_name}{org.main_org ? t('（主组织）') : ''}
                </Button>
              ))}
            </div>
          </div>
        </div>
      )}

      {status?.status === 'error' && status.error && (
        <div className="jx-conn-note jx-conn-note--error">{status.error}</div>
      )}
    </div>
  );
}
