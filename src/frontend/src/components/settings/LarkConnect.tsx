import { useMemo } from 'react';
import { message } from 'antd';
import {
  getLarkStatus,
  startLarkLogin,
  pollLarkLogin,
  disconnectLark,
  type LarkStatus,
  type RequestTarget,
} from '../../api';
import { t } from '../../i18n';
import { AccountConnectPanel, DeviceFlowNotes, DeviceFlowPendingBody } from './AccountConnectPanel';
import { useDeviceFlowConnection, type DeviceFlowApi } from './useDeviceFlowConnection';

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
export function LarkConnect({ target }: { target?: RequestTarget } = {}) {
  const api = useMemo<DeviceFlowApi<LarkStatus>>(() => ({
    getStatus: (probe) => getLarkStatus(probe, target),
    start: () => startLarkLogin(target),
    poll: () => pollLarkLogin(target),
    disconnect: () => disconnectLark(target),
  }), [target]);
  const conn = useDeviceFlowConnection({
    api,
    messages: {
      loadFailed: t('获取飞书连接状态失败'),
      startFailed: t('发起飞书登录失败'),
      connected: t('飞书账号已连接'),
      disconnected: t('已断开飞书连接'),
      disconnectFailed: t('断开失败'),
    },
    onStarted: (s) => {
      if (s.status === 'error' && s.last_error) {
        message.warning(s.last_error);
      } else if (s.status === 'pending' && !s.verification_url && !s.qr_data_uri) {
        message.warning(s.last_error || t('未能获取授权二维码，请重试'));
      }
    },
  });
  const { status } = conn;
  const pending = status?.status === 'pending';

  return (
    <AccountConnectPanel
      provider="lark"
      logo={(
        <svg viewBox="0 0 1024 1024" width="22" height="22" aria-hidden>
          <path fill="currentColor" d="M512 96C282.6 96 96 282.6 96 512s186.6 416 416 416 416-186.6 416-416S741.4 96 512 96zm205 287c-44 96-112 178-198 238 26 18 58 29 93 29 21 0 41-4 60-11l-19 60c-15 4-31 6-47 6-62 0-118-25-159-66-71 35-151 55-235 56l16-58c63-2 124-17 178-43-32-39-55-86-66-138l60-12c9 42 28 80 55 110 70-46 127-112 165-191l-201 1 14-50 257-1c8 0 14 9 11 16z"/>
        </svg>
      )}
      title={t('飞书工作台')}
      description={t('连接你的飞书账号后，智能体可在「飞书」技能里以你的身份操作消息、云文档、多维表格、日历、邮箱、任务、知识库、会议等。凭据安全保存在服务端，仅你本人可用。')}
      badge={STATUS_META[status?.status || 'disconnected']}
      busy={pending}
      connected={status?.status === 'connected'}
      account={status?.lark_name}
      connectLabel={t('连接飞书账号')}
      disconnectConfirm={t('确认断开飞书连接？将清除服务端保存的凭据。')}
      connecting={conn.connecting}
      loading={conn.loading}
      onConnect={() => { void conn.connect(); }}
      onDisconnect={() => { void conn.disconnect(); }}
      onRefresh={() => { void conn.refresh(true); }}
    >
      {pending && status && (
        <DeviceFlowPendingBody
          status={status}
          qrAlt={t('飞书授权二维码')}
          qrTip={t('用飞书扫一扫绑定')}
          title={t('用飞书 App 扫描左侧二维码，确认授权即可绑定。')}
        />
      )}
      {status && <DeviceFlowNotes status={status} />}
    </AccountConnectPanel>
  );
}
