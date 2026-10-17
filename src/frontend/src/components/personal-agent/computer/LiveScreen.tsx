import { useEffect, useRef, useState } from 'react';
import RFB from '@novnc/novnc';
import { webSocketUrl } from '../../../api';
import { personalApi } from '../api';
import { ALT, ALT_KEYS, CONTROL, CONTROL_KEYS, MODIFIERS, charKeysym, eventKeysym } from './keysyms';
import { t } from '../../../i18n';

interface LiveScreenProps {
  /** Empty while watching. With a control token the stream also carries keyboard and mouse. */
  controlToken: string;
}

const MAX_PASTE = 5000;
const MAX_RETRY_DELAY = 5000;
const RETRY_STEP = 600;
// noVNC encoding settings: JPEG quality 0–9 and zlib level 0–9. The hop to the backend is fast, so favor quality.
const QUALITY = 7;
const COMPRESSION = 2;

/** The cloud screen as a live VNC stream. Frames arrive as the screen changes; input goes out at once. */
export function LiveScreen({ controlToken }: LiveScreenProps) {
  const host = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const client = useRef<RFB | undefined>(undefined);
  const held = useRef(new Map<string, number>());
  const [connected, setConnected] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [poster, setPoster] = useState('');
  const controlling = !!controlToken;

  // A cached screenshot fills the frame until the first live frame arrives.
  useEffect(() => {
    const controller = new AbortController();
    void personalApi.preview(controller.signal).then(value => setPoster(value.image)).catch(() => undefined);
    return () => controller.abort();
  }, []);

  useEffect(() => {
    let disposed = false;
    let failures = 0;
    let timer: ReturnType<typeof setTimeout>;
    const retry = () => {
      failures += 1;
      setRetrying(true);
      timer = setTimeout(() => void connect(), Math.min(MAX_RETRY_DELAY, RETRY_STEP * failures));
    };
    const connect = async () => {
      let url: string;
      try { url = webSocketUrl(personalApi.streamPath((await personalApi.streamTicket(controlToken || undefined)).ticket)); }
      catch { if (!disposed) retry(); return; }
      if (disposed || !host.current) return;
      const rfb = new RFB(host.current, url, { shared: true });
      rfb.viewOnly = !controlToken;
      rfb.scaleViewport = true;
      rfb.resizeSession = false;
      // Focus stays in the hidden input, so the browser IME works for Chinese and other languages.
      rfb.focusOnClick = false;
      rfb.qualityLevel = QUALITY;
      rfb.compressionLevel = COMPRESSION;
      rfb.background = 'transparent';
      rfb.addEventListener('connect', () => { failures = 0; setConnected(true); setRetrying(false); });
      rfb.addEventListener('disconnect', () => {
        if (client.current === rfb) client.current = undefined;
        if (disposed) return;
        setConnected(false);
        retry();
      });
      client.current = rfb;
    };
    void connect();
    return () => {
      disposed = true;
      clearTimeout(timer);
      client.current?.disconnect();
      client.current = undefined;
      setConnected(false);
    };
  }, [controlToken]);

  useEffect(() => { if (controlling) input.current?.focus({ preventScroll: true }); }, [controlling]);

  function send(keysym: number, code: string | null, down?: boolean) { client.current?.sendKey(keysym, code, down); }
  function typeText(text: string) {
    for (const char of Array.from(text.replace(/\r\n?/g, '\n')).slice(0, MAX_PASTE)) send(charKeysym(char), null);
  }
  function releaseHeld() {
    held.current.forEach((keysym, code) => send(keysym, code, false));
    held.current.clear();
  }
  function onKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    const native = event.nativeEvent;
    // Leave composition to the IME. The committed text arrives through the input event.
    if (native.isComposing || native.key === 'Process' || native.keyCode === 229) return;
    // Cmd/Ctrl+V pastes the local clipboard: let the browser raise the paste event.
    if ((native.ctrlKey || native.metaKey) && native.key.toLowerCase() === 'v') return;
    const keysym = eventKeysym(native);
    if (!keysym) return;
    event.preventDefault();
    if (MODIFIERS.has(native.key)) {
      held.current.set(native.code || native.key, keysym);
      send(keysym, native.code, true);
    } else {
      // A shortcut can arrive without its modifier's own keydown (focus moved in, or synthetic events).
      const down = [...held.current.values()];
      const wrap = [
        ...((native.ctrlKey || native.metaKey) && !down.some(key => CONTROL_KEYS.has(key)) ? [CONTROL] : []),
        ...(native.altKey && !down.some(key => ALT_KEYS.has(key)) ? [ALT] : []),
      ];
      wrap.forEach(key => send(key, null, true));
      // Press and release together: macOS sends no keyup for keys pressed while Command is held.
      send(keysym, native.code);
      wrap.reverse().forEach(key => send(key, null, false));
    }
  }
  function onKeyUp(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    const code = event.nativeEvent.code || event.nativeEvent.key;
    const keysym = held.current.get(code);
    if (keysym) { send(keysym, event.nativeEvent.code, false); held.current.delete(code); }
  }
  // Committed IME text and other text input. Whichever event comes first consumes the value.
  function flushInput() {
    const target = input.current;
    if (!target || !target.value) return;
    typeText(target.value);
    target.value = '';
  }

  return <div className={'pa-screen' + (controlling ? ' pa-screen--control' : '')} role={controlling ? 'application' : undefined} aria-label={t('云电脑屏幕')}
    onMouseDown={() => { if (controlling) requestAnimationFrame(() => input.current?.focus({ preventScroll: true })); }}>
    <div className="pa-screen-frame">
      {!connected && (poster ? <img className="pa-screen-poster" src={poster} alt="" draggable={false} /> : <div className="pa-screen-loading jx-skeletonBlock" />)}
      <div ref={host} className={'pa-live-host' + (connected ? ' pa-live-host--ready' : '')} />
      {!connected && <span className="pa-screen-status" role="status">{retrying ? t('连接中断，正在重新连接。') : t('正在连接云电脑')}</span>}
    </div>
    {controlling && <textarea ref={input} className="pa-screen-input" aria-label={t('向云电脑输入')} autoComplete="off" autoCorrect="off" autoCapitalize="off" spellCheck={false}
      onKeyDown={onKeyDown} onKeyUp={onKeyUp} onBlur={releaseHeld}
      onInput={event => { if (!(event.nativeEvent as InputEvent).isComposing) flushInput(); }}
      onCompositionEnd={flushInput}
      onPaste={event => { event.preventDefault(); typeText(event.clipboardData.getData('text')); }} />}
  </div>;
}
