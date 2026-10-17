import { useEffect, useState } from 'react';
import { Button, Checkbox, Dropdown, Input, Modal, message } from 'antd';
import type { MenuProps } from 'antd';
import { DesktopOutlined, LaptopOutlined, MoreOutlined, PlusOutlined } from '@ant-design/icons';
import { deviceLinkApi, personalApi, type DeviceConsent, type DeviceLinkStatus, type PersonalDevice, errorText } from '../api';
import { useDeploymentModeStore } from '../../../stores/deploymentModeStore';
import { t } from '../../../i18n';
import { useCanvasStore } from '../../../stores/canvasStore';
import { COMPUTER_CANVAS } from './canvasIds';
import './ComputerCanvas';
import { DeviceSessions } from './DeviceSessions';

interface PersonalComputersProps {
  agentName: string;
  devices: PersonalDevice[];
  status?: string;
  onChange: () => Promise<void>;
}

/** Only the desktop app in dual mode runs a local backend that can link this computer. */
function useCanLinkThisComputer() {
  return useDeploymentModeStore(s => s.isDesktop && s.provisionMode === 'dual' && s.localReady);
}

function deviceNote(device: PersonalDevice, local: DeviceLinkStatus | null) {
  if (!device.online) return t('离线');
  const link = local?.link?.device_id === device.id ? local.link : null;
  if (link?.control_enabled && local?.permissions && !(local.permissions.screen && local.permissions.input)) {
    return t('已连接') + ' · ' + t('等待系统授权');
  }
  if (link && !link.control_enabled && !link.sessions_enabled) return t('已连接') + ' · ' + t('未开启任何许可');
  if (!link && !device.control_enabled) return t('已连接') + ' · ' + t('等待本机允许接管');
  return t('已连接');
}

/** Cloud computer with a live thumbnail, then linked local computers. */
export function PersonalComputers({ agentName, devices, status, onChange }: PersonalComputersProps) {
  const ready = status === 'ready';
  const computerNote = status === 'degraded' ? t('连接异常，正在重试')
    : status === 'recovering' ? t('正在恢复')
    : status === 'not_provisioned' ? t('等待启动')
    : ready ? t('已连接') : t('准备中');
  const canLink = useCanLinkThisComputer();
  const [local, setLocal] = useState<DeviceLinkStatus | null>(null);
  const [sessions, setSessions] = useState<PersonalDevice | null>(null);
  const [pairing, setPairing] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [name, setName] = useState('');
  const [consent, setConsent] = useState<Required<DeviceConsent>>({ control_enabled: false, sessions_enabled: false });
  const [preview, setPreview] = useState<string>();

  useEffect(() => {
    if (!ready) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      // Hidden tabs do not need a fresh screen.
      if (document.visibilityState === 'visible') {
        try { setPreview((await personalApi.preview(controller.signal)).image); } catch { /* keep the last frame */ }
      }
      if (!controller.signal.aborted) timer = setTimeout(() => void load(), 30000);
    };
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [ready]);

  useEffect(() => {
    if (!canLink) { setLocal(null); return; }
    let live = true;
    deviceLinkApi.status().then(status => { if (live) setLocal(status); }).catch(() => { if (live) setLocal(null); });
    return () => { live = false; };
  }, [canLink]);

  // A link whose device was revoked elsewhere no longer counts: connecting again replaces it.
  const thisDevice = local?.link && devices.some(d => d.id === local.link?.device_id) ? local.link.device_id : null;

  function openPairing() {
    if (!local) return;
    setName(local.default_name);
    setConsent({ control_enabled: false, sessions_enabled: false });
    setPairing(true);
  }

  async function connect() {
    if (!local) return;
    setConnecting(true);
    try {
      const label = name.trim();
      const device = await personalApi.pairDevice(label, local.platform, local.capabilities);
      try {
        setLocal(await deviceLinkApi.link({ device_id: device.id, token: device.token, name: label, ...consent }));
      } catch (exc) {
        await personalApi.revokeDevice(device.id).catch(() => undefined);
        throw exc;
      }
      setPairing(false);
      await onChange();
    } catch (exc) { message.error(errorText(exc)); }
    finally { setConnecting(false); }
  }

  async function updateLocal(change: Promise<DeviceLinkStatus>) {
    try { setLocal(await change); await onChange(); }
    catch (exc) { message.error(errorText(exc)); }
  }

  async function revoke(id: string) {
    try {
      await personalApi.revokeDevice(id);
      if (id === thisDevice) setLocal(await deviceLinkApi.unlink());
      await onChange();
    } catch (exc) { message.error(errorText(exc)); }
  }

  function menu(device: PersonalDevice): MenuProps['items'] {
    const link = device.id === thisDevice ? local?.link : null;
    const permissions = local?.permissions;
    return [
      ...(link && local?.control_supported ? [{
        key: 'control', label: link.control_enabled ? t('关闭屏幕接管') : t('允许接管屏幕'),
        onClick: () => void updateLocal(deviceLinkApi.consent({ control_enabled: !link.control_enabled })),
      }] : []),
      ...(link && permissions && !(permissions.screen && permissions.input) ? [{
        key: 'permissions', label: t('授予系统权限'), onClick: () => void updateLocal(deviceLinkApi.requestPermissions()),
      }] : []),
      ...(link ? [{
        key: 'sessions-consent', label: link.sessions_enabled ? t('关闭会话管理') : t('允许管理会话'),
        onClick: () => void updateLocal(deviceLinkApi.consent({ sessions_enabled: !link.sessions_enabled })),
      }] : []),
      { key: 'sessions', label: t('管理会话'), disabled: !device.online || !device.sessions_enabled, onClick: () => setSessions(device) },
      { key: 'revoke', danger: true, label: t('断开连接'), onClick: () => void revoke(device.id) },
    ];
  }

  return <>
    <button type="button" className="pa-side-row pa-side-row--computer" disabled={!ready} onClick={() => useCanvasStore.getState().openCustom({ renderer: COMPUTER_CANVAS, title: t('{name} 的电脑', { name: agentName }) })} aria-label={t('在右侧查看云电脑')}>
      <span className="pa-side-icon"><DesktopOutlined /><i className={'pa-dot' + (ready ? ' pa-dot--on' : '')} /></span>
      <span className="pa-side-text"><strong title={t('{name} 的电脑', { name: agentName })}>{t('{name} 的电脑', { name: agentName })}</strong><small>{computerNote}</small></span>
      {ready && <span className="pa-side-thumb">{preview ? <img src={preview} alt={t('云电脑屏幕')} /> : <span className="jx-skeletonBlock" />}</span>}
    </button>
    {devices.map(device => <div className="pa-side-row" key={device.id}>
      <span className="pa-side-icon"><LaptopOutlined /><i className={'pa-dot' + (device.online ? ' pa-dot--on' : '')} /></span>
      <span className="pa-side-text"><strong>{device.name}{device.id === thisDevice ? ' · ' + t('本机') : ''}</strong>
        <small>{deviceNote(device, device.id === thisDevice ? local : null)}</small></span>
      <Dropdown trigger={['click']} menu={{ items: menu(device) }}><Button type="text" size="small" icon={<MoreOutlined />} aria-label={t('更多操作')} /></Dropdown>
    </div>)}
    {local && !thisDevice && <button type="button" className="pa-side-link" onClick={openPairing}><PlusOutlined /> {t('连接这台电脑')}</button>}
    {sessions && <DeviceSessions target={'device:' + sessions.id} name={sessions.name} onClose={() => setSessions(null)} />}
    <Modal open={pairing} title={t('连接这台电脑')} onCancel={() => setPairing(false)} footer={null}>
      <div className="pa-pairing">
        <Input aria-label={t('电脑名称')} placeholder={t('电脑名称')} value={name} maxLength={100} onChange={event => setName(event.target.value)} />
        <Checkbox checked={consent.control_enabled} disabled={!local?.control_supported} onChange={event => setConsent({ ...consent, control_enabled: event.target.checked })}>
          {t('允许接管屏幕、鼠标和键盘')}
        </Checkbox>
        {!local?.control_supported && <p className="pa-hint">{t('这台电脑暂不支持屏幕接管。')}</p>}
        <Checkbox checked={consent.sessions_enabled} onChange={event => setConsent({ ...consent, sessions_enabled: event.target.checked })}>
          {t('允许管理本机会话')}
        </Checkbox>
        <p className="pa-hint">{t('许可只能在这台电脑上开启，随时可以在设备菜单中关闭。')}</p>
        <Button type="primary" loading={connecting} disabled={!name.trim()} onClick={() => void connect()}>{t('连接')}</Button>
      </div>
    </Modal>
  </>;
}
