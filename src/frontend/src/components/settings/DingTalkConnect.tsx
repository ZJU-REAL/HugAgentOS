import { useMemo } from 'react';
import { message } from 'antd';
import {
  getDingTalkStatus,
  startDingTalkLogin,
  pollDingTalkLogin,
  disconnectDingTalk,
  type DingTalkStatus,
  type RequestTarget,
} from '../../api';
import { t } from '../../i18n';
import { AccountConnectPanel, DeviceFlowNotes, DeviceFlowPendingBody } from './AccountConnectPanel';
import { useDeviceFlowConnection, type DeviceFlowApi } from './useDeviceFlowConnection';

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
export function DingTalkConnect({ target }: { target?: RequestTarget } = {}) {
  const api = useMemo<DeviceFlowApi<DingTalkStatus>>(() => ({
    getStatus: (probe) => getDingTalkStatus(probe, target),
    start: () => startDingTalkLogin(target),
    poll: () => pollDingTalkLogin(target),
    disconnect: () => disconnectDingTalk(target),
  }), [target]);
  const conn = useDeviceFlowConnection({
    api,
    messages: {
      loadFailed: t('获取钉钉连接状态失败'),
      startFailed: t('发起钉钉登录失败'),
      connected: t('钉钉账号已连接'),
      disconnected: t('已断开钉钉连接'),
      disconnectFailed: t('断开失败'),
    },
    onStarted: (s) => {
      if (s.status === 'pending' && !s.verification_url) {
        message.warning(s.last_error || t('未能获取授权链接，请重试'));
      }
    },
  });
  const { status } = conn;
  const pending = status?.status === 'pending';

  return (
    <AccountConnectPanel
      provider="dingtalk"
      logo={(
        <svg viewBox="0 0 1024 1024" width="22" height="22" aria-hidden>
          <path fill="currentColor" d="M512 96C282.6 96 96 282.6 96 512s186.6 416 416 416 416-186.6 416-416S741.4 96 512 96zm236 348c-12 47-50 116-50 116l1 1c-15 25-39 60-72 96l16-127-94 19c0-3 53-150 53-150L394 432s213-83 232-90c5-2 11-1 14 1 6 4 5 10 3 18-2 6-37 96-37 96l59-13c4-1 8 0 11 3 4 4 4 10 2 15-7 16-26 56-43 90l1 1c20-4 35-7 35-7s27-6 24 8z"/>
        </svg>
      )}
      title={t('钉钉工作台')}
      description={t('连接你的钉钉账号后，智能体可在「钉钉工作台」技能里以你的身份操作通讯录、日历、待办、审批、钉钉文档、群聊等。凭据安全保存在服务端，仅你本人可用。')}
      badge={STATUS_META[status?.status || 'disconnected']}
      busy={pending}
      connected={status?.status === 'connected'}
      account={status?.dingtalk_name ? `${status.dingtalk_name}${status.corp_id ? ` · ${status.corp_id}` : ''}` : null}
      connectLabel={t('连接钉钉账号')}
      disconnectConfirm={t('确认断开钉钉连接？将清除服务端保存的凭据。')}
      connecting={conn.connecting}
      loading={conn.loading}
      onConnect={() => { void conn.connect(); }}
      onDisconnect={() => { void conn.disconnect(); }}
      onRefresh={() => { void conn.refresh(true); }}
    >
      {pending && status && (
        <DeviceFlowPendingBody
          status={status}
          qrAlt={t('钉钉授权二维码')}
          qrTip={t('用钉钉扫一扫绑定')}
          title={t('用钉钉 App 扫描左侧二维码，确认授权即可绑定。')}
        />
      )}
      {status && <DeviceFlowNotes status={status} />}
    </AccountConnectPanel>
  );
}
