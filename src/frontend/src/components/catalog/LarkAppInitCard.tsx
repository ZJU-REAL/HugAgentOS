import { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, Popconfirm, Space, Spin, Typography, message } from 'antd';

import {
  getLarkAppInitStatus,
  resetLarkAppInit,
  startLarkAppInit,
  type LarkAppInitStatus,
  type RequestTarget,
} from '../../api';
import { usePolling } from '../../hooks/usePolling';
import { t } from '../../i18n';

const { Text } = Typography;

/** The three calls the card needs. Defaults to the plugin API; the admin console passes its own fetch. */
export interface LarkAppInitClient {
  status: () => Promise<LarkAppInitStatus>;
  start: () => Promise<LarkAppInitStatus>;
  reset: () => Promise<LarkAppInitStatus>;
}

interface LarkAppInitCardProps {
  target?: RequestTarget;
  client?: LarkAppInitClient;
  /**
   * `settings` (default): titled section in the plugin's account settings.
   * `inline`: untitled block inside the admin "Lark workspace" config card.
   */
  variant?: 'settings' | 'inline';
}

/** CE/EE instance-admin control for the org-wide Feishu app used by the plugin. */
export function LarkAppInitCard({ target, client, variant = 'settings' }: LarkAppInitCardProps = {}) {
  const api = useMemo<LarkAppInitClient>(() => client ?? {
    status: () => getLarkAppInitStatus(target),
    start: () => startLarkAppInit(target),
    reset: () => resetLarkAppInit(target),
  }, [client, target]);
  const [status, setStatus] = useState<LarkAppInitStatus | null>(null);
  const [accessible, setAccessible] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setStatus(await api.status());
      setAccessible(true);
    } catch {
      // Regular users do not have system-settings permission. They still see
      // the personal account connection below, but not the instance-wide app control.
      setAccessible(false);
    }
  }, [api]);

  useEffect(() => { void load(); }, [load]);

  const pending = status?.status === 'pending';
  usePolling(load, { interval: 3000, enabled: pending });

  const handleInit = useCallback(async () => {
    setBusy(true);
    try {
      setStatus(await api.start());
    } catch (error) {
      message.error(t('初始化失败：{msg}', { msg: (error as Error).message }));
    } finally {
      setBusy(false);
    }
  }, [api]);

  const handleReset = useCallback(async () => {
    setBusy(true);
    try {
      setStatus(await api.reset());
    } catch (error) {
      message.error(t('初始化失败：{msg}', { msg: (error as Error).message }));
    } finally {
      setBusy(false);
    }
  }, [api]);

  if (accessible !== true) return null;

  const configured = status?.configured === true;
  const inline = variant === 'inline';

  const body = !status ? (
    <Spin size="small" />
  ) : configured ? (
    <Space orientation="vertical" size={6} style={{ width: '100%' }}>
      <Text strong style={{ color: '#52c41a' }}>{t('飞书应用已配置')}</Text>
      <Text type="secondary" style={{ fontSize: 12 }}>App ID: {status.app_id || '-'}</Text>
      <Text type="secondary" style={{ fontSize: 12 }}>
        {inline
          ? t('全组共用此应用，用户在「设置 → 集成」扫码登录各自账号即可。')
          : t('全组共用此应用，用户可在下方「账号连接」区域扫码登录各自账号。')}
      </Text>
      <Popconfirm
        title={t('确认重新初始化飞书应用？')}
        onConfirm={() => { void handleReset(); }}
        okText={t('重新初始化')}
        cancelText={t('取消')}
      >
        <Button danger size="small" loading={busy}>{t('重新初始化')}</Button>
      </Popconfirm>
    </Space>
  ) : pending && (status.qr_data_uri || status.verification_url) ? (
    <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start', flexWrap: 'wrap' }}>
      {status.qr_data_uri && (
        <img
          src={status.qr_data_uri}
          alt={t('飞书应用配置二维码')}
          style={{ width: 160, height: 160, background: 'var(--color-bg-container)', padding: 8, borderRadius: 8 }}
        />
      )}
      <Space orientation="vertical" size={8} style={{ flex: 1, minWidth: 200 }}>
        <Text strong>{t('用管理员的飞书 App 扫码完成应用配置（仅需一次）。')}</Text>
        {status.verification_url && (
          <a href={status.verification_url} target="_blank" rel="noreferrer">
            {t('或点此在浏览器中打开配置页')}
          </a>
        )}
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t('完成后自动就绪，无需手动刷新。')}
        </Text>
      </Space>
    </div>
  ) : pending ? (
    <Space><Spin size="small" /><Text>{t('正在获取授权二维码…')}</Text></Space>
  ) : (
    <Space orientation="vertical" size={6} style={{ width: '100%' }}>
      <Text>{t('一键初始化全组共用的飞书应用，无需手动创建应用或填写凭据。')}</Text>
      {status.error && <Text type="danger" style={{ fontSize: 12 }}>{status.error}</Text>}
      <Button type="primary" loading={busy} onClick={() => { void handleInit(); }}>
        {t('初始化飞书应用')}
      </Button>
    </Space>
  );

  if (inline) {
    return (
      <div style={{ marginBottom: 12, padding: 12, background: 'var(--color-bg-gray)', borderRadius: 8 }}>
        {body}
      </div>
    );
  }
  return (
    <div style={{ marginTop: 12 }}>
      <h4 className="jx-sectionTitle">{t('初始化飞书应用')}</h4>
      <div className="jx-settings-card">{body}</div>
    </div>
  );
}
