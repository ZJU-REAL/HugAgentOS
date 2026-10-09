import type { WeixinBindStart, WeixinBindStatus } from '../../api';
import { t } from '../../i18n';

export interface WeixinBindingState {
  open: boolean;
  phase: 'idle' | 'loading' | 'waiting' | 'scanned' | 'error' | 'expired';
  image: string;
  tip: string;
}

export const INITIAL_WEIXIN_BINDING: WeixinBindingState = {
  open: false, phase: 'idle', image: '', tip: '',
};

interface BindingActions {
  start: () => Promise<WeixinBindStart>;
  poll: (bindId: string) => Promise<WeixinBindStatus>;
  onChange: (state: WeixinBindingState) => void;
  onConfirmed: () => Promise<void>;
}

/** One QR attempt owns its timers and late responses; long polls never overlap. */
export function createWeixinBindingFlow(actions: BindingActions) {
  let attempt = 0;
  let active = false;
  let state = INITIAL_WEIXIN_BINDING;
  let pollTimer: ReturnType<typeof setTimeout> | undefined;
  let expiryTimer: ReturnType<typeof setTimeout> | undefined;

  const clearTimers = () => {
    clearTimeout(pollTimer);
    clearTimeout(expiryTimer);
    pollTimer = expiryTimer = undefined;
  };
  const publish = (patch: Partial<WeixinBindingState>) => {
    state = { ...state, ...patch };
    actions.onChange(state);
  };
  const current = (id: number) => active && id === attempt;
  const finish = (phase: 'error' | 'expired', tip: string) => {
    active = false;
    clearTimers();
    publish({ phase, tip });
  };
  const close = () => {
    active = false;
    attempt += 1;
    clearTimers();
    publish(INITIAL_WEIXIN_BINDING);
  };

  const poll = async (id: number, bindId: string) => {
    if (!current(id)) return;
    try {
      const result = await actions.poll(bindId);
      if (!current(id)) return;
      if (result.status === 'confirmed') {
        if (!result.channel_id) throw new Error(t('绑定失败，请重试'));
        close();
        await actions.onConfirmed();
        return;
      }
      if (result.status === 'expired') {
        finish('expired', t('二维码已过期，请重试'));
        return;
      }
      if (result.status !== 'waiting' && result.status !== 'scanned') {
        throw new Error(t('绑定失败，请重试'));
      }
      publish({
        phase: result.status,
        tip: result.status === 'scanned'
          ? t('已扫描，请在手机上确认') : t('请用微信扫描二维码并确认登录'),
      });
      pollTimer = setTimeout(() => { void poll(id, bindId); }, 2000);
    } catch (error) {
      if (current(id)) finish('error', (error as Error)?.message || t('绑定失败，请重试'));
    }
  };

  const start = async () => {
    clearTimers();
    const id = ++attempt;
    active = true;
    publish({ open: true, phase: 'loading', image: '', tip: t('正在获取二维码…') });
    // Expiry also bounds a stalled HTTP request. Late responses are ignored.
    expiryTimer = setTimeout(() => {
      if (current(id)) finish('expired', t('二维码已过期，请重试'));
    }, 300_000);
    try {
      const result = await actions.start();
      if (!current(id)) return;
      if (!result?.bind_id || !result.qrcode_img) throw new Error(t('获取二维码失败'));
      publish({ phase: 'waiting', image: result.qrcode_img, tip: t('请用微信扫描二维码并确认登录') });
      pollTimer = setTimeout(() => { void poll(id, result.bind_id); }, 2000);
    } catch (error) {
      if (current(id)) finish('error', (error as Error)?.message || t('获取二维码失败'));
    }
  };

  // Dispose without publishing React state during unmount.
  const dispose = () => { active = false; attempt += 1; clearTimers(); };
  return { start, close, dispose };
}
