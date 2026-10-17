import { useEffect, useRef, useState, type RefObject } from 'react';
import { useAuthStore } from '../../stores/authStore';
import { ApiResponseError } from '../../utils/apiError';
import { getAudioStatus, transcribeAudio } from '../../api/audio';
import { VoiceCapture } from './voiceCapture';
import { voiceAnchor } from './voiceAnchor';
import { t } from '../../i18n';

export type VoicePhase = 'idle' | 'connecting' | 'recording' | 'processing' | 'error';
type Session = { capture: VoiceCapture; abort: AbortController; insert: (text: string) => boolean; blob?: Blob; text?: string };

export function useVoiceInput(contextKey: string, input: RefObject<HTMLElement | null>, accountInput = true) {
  const allowed = useAuthStore(s => accountInput && s.authUser?.can_use_voice === true);
  const [phase, setPhase] = useState<VoicePhase>('idle');
  const [stream, setStream] = useState<MediaStream | null>(null);
  const [error, setError] = useState('');
  const [result, setResult] = useState('');
  const [seconds, setSeconds] = useState(0);
  const current = useRef<Session | null>(null);
  const cancel = () => {
    const session = current.current;
    current.current = null;
    session?.abort.abort(); session?.capture.close();
    setStream(null); setPhase('idle'); setError(''); setResult(''); setSeconds(0);
  };
  useEffect(() => {
    queueMicrotask(() => { if (!current.current) { setPhase('idle'); setStream(null); setError(''); setResult(''); setSeconds(0); } });
    // No late permission grant or transcript may write into a different chat/account.
    return () => { const session = current.current; current.current = null; session?.abort.abort(); session?.capture.close(); };
  }, [contextKey, allowed]);
  useEffect(() => {
    if (phase !== 'recording') return;
    const start = Date.now();
    const timer = setInterval(() => setSeconds(Math.floor((Date.now() - start) / 1000)), 200);
    return () => clearInterval(timer);
  }, [phase]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && current.current) { event.preventDefault(); cancel(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  useEffect(() => {
    if (!allowed || (phase !== 'recording' && phase !== 'processing')) return;
    const userId = useAuthStore.getState().authUser?.user_id;
    let request: AbortController | null = null;
    const timer = setInterval(async () => {
      if (request) return;
      const abort = new AbortController(); request = abort;
      const timeout = setTimeout(() => abort.abort(), 4000);
      try {
        const status = await getAudioStatus(abort.signal);
        if (!abort.signal.aborted && !status.allowed) {
          useAuthStore.setState(state => state.authUser && state.authUser.user_id === userId
            ? { authUser: { ...state.authUser, can_use_voice: false } } : state);
        }
      } catch { /* A transient network error does not grant or extend permission. */ }
      finally { clearTimeout(timeout); request = null; }
    }, 5000);
    return () => { clearInterval(timer); request?.abort(); };
  }, [allowed, phase]);

  const fail = (session: Session, value: unknown) => {
    if (current.current !== session) return;
    session.capture.close(); setStream(null);
    const err = value as Error;
    setError(err.name === 'NotAllowedError' ? t('麦克风权限被拒绝，请在浏览器或系统设置中允许访问')
      : err.name === 'NotFoundError' ? t('未找到麦克风，请连接设备后重试') : err.message || t('录音失败，请重试'));
    setPhase('error');
  };
  const upload = async (session: Session) => {
    setPhase('processing'); setError('');
    session.abort = new AbortController();
    const timeout = setTimeout(() => session.abort.abort(), 70000);
    try {
      const text = await transcribeAudio(session.blob!, session.abort.signal);
      if (current.current !== session) return;
      session.text = text;
      if (!session.insert(text)) {
        setResult(text); setError(t('输入内容已变化，转写文字已保留，请选择插入或复制')); setPhase('error'); return;
      }
      current.current = null; setPhase('idle'); setResult('');
    } catch (value) {
      if (current.current !== session) return;
      // Invalid/empty audio needs a new recording; transport failures can reuse it.
      if (value instanceof ApiResponseError && [413, 415, 422].includes(value.status)) session.blob = undefined;
      fail(session, (value as Error).name === 'AbortError' ? new Error(t('语音转写超时，请重试')) : value);
    } finally { clearTimeout(timeout); }
  };
  const finish = () => {
    const session = current.current;
    if (!session || !session.capture.stream) return;
    session.blob = session.capture.stop(); setStream(null);
    void upload(session);
  };
  const start = async () => {
    if (!allowed || current.current) return;
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      setError(t('语音输入需要 HTTPS 或本机 localhost 安全连接')); setPhase('error'); return;
    }
    if (!window.AudioContext || !window.AudioWorkletNode) {
      setError(t('当前浏览器不支持录音，请升级浏览器后重试')); setPhase('error'); return;
    }
    const session: Session = { capture: new VoiceCapture(), abort: new AbortController(), insert: voiceAnchor(input) };
    current.current = session; setPhase('connecting'); setError(''); setResult(''); setSeconds(0);
    let startupTimedOut = false;
    const timeout = setTimeout(() => {
      startupTimedOut = true; session.abort.abort();
      fail(session, new Error(t('麦克风连接超时，请确认权限后重试')));
    }, 10000);
    try {
      const status = await getAudioStatus(session.abort.signal);
      if (!status.allowed) throw new Error(t('未开通语音输入权限，请联系管理员'));
      if (!status.configured) throw new Error(t('尚未配置音频转写模型，请管理员在模型管理中配置'));
      if (current.current !== session || startupTimedOut) return;
      const stream = await session.capture.start(finish, () => fail(session, new Error(t('麦克风连接已断开，请重新录音'))));
      if (current.current !== session) { session.capture.close(); return; }
      setStream(stream); setPhase('recording');
    } catch (value) { if (!startupTimedOut) fail(session, value); }
    finally { clearTimeout(timeout); }
  };
  const retry = () => {
    const session = current.current;
    if (session?.blob) { void upload(session); return; }
    cancel(); void start();
  };
  const insertResult = () => {
    if (!result || !voiceAnchor(input)(result)) return;
    cancel();
  };
  return { allowed, phase, stream, error, result, seconds, busy: phase !== 'idle' && phase !== 'error', start, finish, cancel, retry, insertResult };
}
export type VoiceInputState = ReturnType<typeof useVoiceInput>;
