import { AudioOutlined, CheckOutlined, CloseOutlined, CopyOutlined, ReloadOutlined } from '@ant-design/icons';
import type { VoiceInputState } from './useVoiceInput';
import { LiveMicActivityBars } from './LiveMicActivityBars';
import { t } from '../../i18n';
import '../../styles/voice-input.css';

export function VoiceInput({ voice }: { voice: VoiceInputState }) {
  const { phase, error, result, seconds, stream } = voice;
  const active = voice.busy;
  if (!voice.allowed) return null;
  return <div className={`jx-voiceInput${active ? ' is-active' : ''}`}>
    {!active && <button type="button" className="jx-voiceMic" aria-label={t('语音输入')} title={t('语音输入')}
      onPointerDown={event => event.preventDefault()} onClick={() => { voice.cancel(); void voice.start(); }}><AudioOutlined /></button>}
    {active && <div className="jx-voiceControls" aria-label={t('语音输入')}>
      <LiveMicActivityBars stream={stream} waiting={phase !== 'recording'} />
      <span className="jx-voiceStatus" role="status">{phase === 'connecting' ? t('正在连接麦克风') : phase === 'processing' ? t('正在转成文字') : `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`}</span>
      {phase === 'recording' && <button type="button" aria-label={t('完成录音')} title={t('完成录音')} onClick={voice.finish}><CheckOutlined /></button>}
      <button type="button" aria-label={t('取消录音')} title={t('取消录音')} onClick={voice.cancel}><CloseOutlined /></button>
    </div>}
    {phase === 'error' && <div className="jx-voiceError">
      <div role="alert">{error}</div>
      {result && <textarea aria-label={t('转写结果')} value={result} readOnly />}
      <div className="jx-voiceErrorActions">
        {result ? <>
          <button type="button" onClick={voice.insertResult}>{t('插入文字')}</button>
          <button type="button" onClick={() => { void navigator.clipboard.writeText(result).catch(() => {}); }}><CopyOutlined />{t('复制')}</button>
        </> : <button type="button" aria-label={t('重试')} onClick={voice.retry}><ReloadOutlined />{t('重试')}</button>}
        <button type="button" onClick={voice.cancel}>{t('关闭')}</button>
      </div>
    </div>}
  </div>;
}
