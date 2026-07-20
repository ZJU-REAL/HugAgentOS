import { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Popconfirm, Space, Spin, Typography, message } from 'antd';

import {
  getLarkAppInitStatus,
  resetLarkAppInit,
  startLarkAppInit,
  type LarkAppInitStatus,
} from '../../api';
import { t } from '../../i18n';

const { Text } = Typography;

/** CE/EE instance-admin control for the org-wide Feishu app used by the plugin. */
export function LarkAppInitCard() {
  const [status, setStatus] = useState<LarkAppInitStatus | null>(null);
  const [accessible, setAccessible] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const load = useCallback(async () => {
    try {
      setStatus(await getLarkAppInitStatus());
      setAccessible(true);
    } catch {
      // Regular users do not have system-settings permission. They still see
      // the personal account connection below, but not the instance-wide app control.
      setAccessible(false);
    }
  }, []);

  useEffect(() => {
    void load();
    return () => stopPolling();
  }, [load, stopPolling]);

  useEffect(() => {
    if (status?.status === 'pending' && !pollRef.current) {
      pollRef.current = setInterval(() => { void load(); }, 3000);
    }
    if (status && status.status !== 'pending') stopPolling();
    return undefined;
  }, [load, status, stopPolling]);

  const handleInit = useCallback(async () => {
    setBusy(true);
    try {
      setStatus(await startLarkAppInit());
    } catch (error) {
      message.error(t('初始化失败：{msg}', { msg: (error as Error).message }));
    } finally {
      setBusy(false);
    }
  }, []);

  const handleReset = useCallback(async () => {
    stopPolling();
    setBusy(true);
    try {
      setStatus(await resetLarkAppInit());
    } catch (error) {
      message.error(t('初始化失败：{msg}', { msg: (error as Error).message }));
    } finally {
      setBusy(false);
    }
  }, [stopPolling]);

  if (accessible !== true) return null;

  const configured = status?.configured === true;
  const pending = status?.status === 'pending';

  return (
    <div style={{ marginTop: 12 }}>
      <h4 className="jx-sectionTitle">{t('初始化飞书应用')}</h4>
      <div className="jx-settings-card">
        {!status ? (
          <Spin size="small" />
        ) : configured ? (
          <Space direction="vertical" size={6} style={{ width: '100%' }}>
            <Text strong style={{ color: '#52c41a' }}>{t('飞书应用已配置')}</Text>
            <Text type="secondary" style={{ fontSize: 12 }}>App ID: {status.app_id || '-'}</Text>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t('全组共用此应用，用户可在下方「账号连接」区域扫码登录各自账号。')}
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
                style={{ width: 160, height: 160, background: '#fff', padding: 8, borderRadius: 8 }}
              />
            )}
            <Space direction="vertical" size={8} style={{ flex: 1, minWidth: 200 }}>
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
          <Space direction="vertical" size={6} style={{ width: '100%' }}>
            <Text>{t('一键初始化全组共用的飞书应用，无需手动创建应用或填写凭据。')}</Text>
            {status.error && <Text type="danger" style={{ fontSize: 12 }}>{status.error}</Text>}
            <Button type="primary" loading={busy} onClick={() => { void handleInit(); }}>
              {t('初始化飞书应用')}
            </Button>
          </Space>
        )}
      </div>
    </div>
  );
}
