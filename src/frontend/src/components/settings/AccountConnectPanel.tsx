import type { ReactNode } from 'react';
import { Button, Popconfirm } from 'antd';
import {
  CheckCircleFilled, DisconnectOutlined, LinkOutlined, LoadingOutlined, ReloadOutlined,
} from '@ant-design/icons';

import { t } from '../../i18n';

interface AccountConnectPanelProps {
  /** BEM modifier for `.jx-conn--{provider}` and `.jx-conn-logo--{provider}`. */
  provider: string;
  logo: ReactNode;
  title: string;
  description: string;
  /** Badge class and label for the current status. */
  badge: { cls: string; label: string };
  /** Shows the spinner badge (pending, or waiting for the user to pick something). */
  busy: boolean;
  connected: boolean;
  account?: ReactNode;
  connectLabel: string;
  disconnectConfirm: string;
  connecting: boolean;
  loading: boolean;
  onConnect: () => void;
  onDisconnect: () => void;
  onRefresh: () => void;
  /** Body under the status bar (QR code, organization picker, error notes). */
  children?: ReactNode;
}

/** Shared shell of the Feishu / DingTalk / Yida account-connection panels. */
export function AccountConnectPanel({
  provider, logo, title, description, badge, busy, connected, account,
  connectLabel, disconnectConfirm, connecting, loading,
  onConnect, onDisconnect, onRefresh, children,
}: AccountConnectPanelProps) {
  return (
    <div className={`jx-conn jx-conn--${provider}`}>
      <div className="jx-conn-head">
        <div className={`jx-conn-logo jx-conn-logo--${provider}`}>{logo}</div>
        <div className="jx-conn-headText">
          <div className="jx-conn-title">{title}</div>
          <div className="jx-conn-desc">{description}</div>
        </div>
      </div>

      <div className="jx-conn-statusBar">
        <div className="jx-conn-statusLeft">
          <span className={`jx-conn-badge jx-conn-badge--${badge.cls}`}>
            {connected ? <CheckCircleFilled />
              : busy ? <LoadingOutlined />
                : <span className="jx-conn-dot" />}
            {badge.label}
          </span>
          {connected && account ? <span className="jx-conn-account">{account}</span> : null}
        </div>
        <div className="jx-conn-actions">
          {connected ? (
            <Popconfirm title={disconnectConfirm} onConfirm={onDisconnect} okText={t('断开')} cancelText={t('取消')}>
              <Button danger icon={<DisconnectOutlined />}>{t('断开连接')}</Button>
            </Popconfirm>
          ) : (
            <Button type="primary" icon={<LinkOutlined />} loading={connecting} onClick={onConnect}>
              {busy ? t('重新发起') : connectLabel}
            </Button>
          )}
          <Button icon={<ReloadOutlined />} onClick={onRefresh} loading={loading}>{t('刷新状态')}</Button>
        </div>
      </div>

      {children}
    </div>
  );
}

interface DeviceFlowPendingStatus {
  qr_data_uri: string | null;
  verification_url: string | null;
  verification_url_complete: string | null;
  user_code: string | null;
  last_error: string | null;
}

/** Pending body for OAuth device flows (QR code, verification link, user code). */
export function DeviceFlowPendingBody({ status, qrAlt, qrTip, title }: {
  status: DeviceFlowPendingStatus;
  qrAlt: string;
  qrTip: string;
  title: string;
}) {
  const link = status.verification_url_complete || status.verification_url;
  return (
    <div className="jx-conn-pending">
      {(status.qr_data_uri || status.verification_url) ? (
        <>
          {status.qr_data_uri && (
            <div className="jx-conn-qr">
              <img src={status.qr_data_uri} alt={qrAlt} />
              <div className="jx-conn-qrTip">{qrTip}</div>
            </div>
          )}
          <div className="jx-conn-pendingMain">
            <div className="jx-conn-pendingTitle">{title}</div>
            {link && (
              <a className="jx-conn-link" href={link} target="_blank" rel="noreferrer">
                <LinkOutlined /> {t('或点此在浏览器中打开授权页')}
              </a>
            )}
            {status.user_code && (
              <div className="jx-conn-code">{t('授权码')}：<code>{status.user_code}</code></div>
            )}
            <div className="jx-conn-hint">{t('授权完成后将自动连接，无需手动刷新。')}</div>
          </div>
        </>
      ) : (
        <div className="jx-conn-note jx-conn-note--warning">{status.last_error || t('正在获取授权二维码…')}</div>
      )}
    </div>
  );
}

/** Error / expired-login notes shared by the OAuth device-flow panels. */
export function DeviceFlowNotes({ status }: { status: { status: string; last_error: string | null } }) {
  if (!status.last_error) return null;
  if (status.status === 'error') {
    return <div className="jx-conn-note jx-conn-note--error">{status.last_error}</div>;
  }
  // After a server-side rotation/expiry the status falls back to disconnected with a note: prompt to reconnect.
  if (status.status === 'disconnected') {
    return <div className="jx-conn-note jx-conn-note--warning">{status.last_error}</div>;
  }
  return null;
}
