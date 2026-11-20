import { useMemo } from 'react';
import { Button, message } from 'antd';
import {
  getYidaStatus,
  startYidaLogin,
  pollYidaLogin,
  disconnectYida,
  type YidaStatus,
  type RequestTarget,
} from '../../api';
import { t } from '../../i18n';
import { AccountConnectPanel } from './AccountConnectPanel';
import { useDeviceFlowConnection, type DeviceFlowApi } from './useDeviceFlowConnection';

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
 * → user scans with DingTalk → frontend long-polls (backend agent-poll blocks ~45s per call; the next
 * poll starts only after the previous one returns) → for multiple organizations, the user picks one and
 * polling resumes with its corp_id → connected.
 * The login cookie is persisted in the server-side Yida working directory; in-chat QR login writes to the same one.
 */
export function YidaConnect({ target }: { target?: RequestTarget } = {}) {
  const api = useMemo<DeviceFlowApi<YidaStatus, string>>(() => ({
    getStatus: (probe) => getYidaStatus(probe, target),
    start: () => startYidaLogin(target),
    poll: (corpId) => pollYidaLogin(corpId, target),
    disconnect: () => disconnectYida(target),
  }), [target]);
  const conn = useDeviceFlowConnection({
    api,
    pollInterval: 1500,
    messages: {
      loadFailed: t('获取宜搭连接状态失败'),
      startFailed: t('发起宜搭登录失败'),
      connected: t('宜搭账号已连接'),
      disconnected: t('已断开宜搭连接'),
      disconnectFailed: t('断开失败'),
    },
    // A pending response without a QR code (old backend / exceptional path) keeps the one on screen,
    // so the panel does not fall back to "Fetching login QR code…".
    mergePolled: (prev, next) => (
      next.status === 'pending' && !next.qr_data_uri && prev?.qr_data_uri
        ? { ...next, qr_data_uri: prev.qr_data_uri, qr_url: prev.qr_url }
        : next
    ),
    onStarted: (s) => {
      if (s.status === 'error') message.error(s.error || t('发起宜搭登录失败'));
    },
  });
  const { status } = conn;
  const pending = status?.status === 'pending';
  const corpSelection = status?.status === 'corp_selection';

  return (
    <AccountConnectPanel
      provider="yida"
      logo={(
        <svg viewBox="0 0 1024 1024" width="22" height="22" aria-hidden>
          <path fill="currentColor" d="M512 64L128 256v512l384 192 384-192V256L512 64zm0 96l288 144-288 144-288-144 288-144zM224 400l256 128v288L224 688V400zm576 288l-256 128V528l256-128v288z"/>
        </svg>
      )}
      title={t('宜搭低代码平台')}
      description={t('用钉钉扫码连接宜搭账号后，智能体可在「宜搭低代码平台」技能里以你的身份创建应用、表单、流程与报表。登录态安全保存在服务端，仅你本人可用。')}
      badge={STATUS_META[status?.status || 'disconnected']}
      busy={pending || corpSelection}
      connected={status?.status === 'connected'}
      account={status?.corp_id}
      connectLabel={t('连接宜搭账号')}
      disconnectConfirm={t('确认断开宜搭连接？将清除服务端保存的登录态。')}
      connecting={conn.connecting}
      loading={conn.loading}
      onConnect={() => { void conn.connect(); }}
      onDisconnect={() => { void conn.disconnect(); }}
      onRefresh={() => { void conn.refresh(true); }}
    >
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
                <Button key={org.corp_id} onClick={() => conn.continueWith(org.corp_id)}>
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
    </AccountConnectPanel>
  );
}
