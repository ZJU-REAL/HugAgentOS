import workletUrl from './capture.worklet.js?url';
import { encodeVoiceWav, MAX_VOICE_SECONDS } from './voiceWav';

export class VoiceCapture {
  stream: MediaStream | null = null;
  private context: AudioContext | null = null;
  private node: AudioWorkletNode | null = null;
  private chunks: Float32Array[] = [];
  private samples = 0;
  private cancelled = false;
  private timer: ReturnType<typeof setTimeout> | undefined;

  async start(onLimit: () => void, onEnded: () => void): Promise<MediaStream> {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }, video: false });
    if (this.cancelled) { stream.getTracks().forEach(track => track.stop()); throw new DOMException('Cancelled', 'AbortError'); }
    this.stream = stream;
    stream.getAudioTracks().forEach(track => { track.onended = onEnded; });
    const context = new AudioContext();
    this.context = context;
    await context.resume();
    await context.audioWorklet.addModule(workletUrl);
    if (this.cancelled) throw new DOMException('Cancelled', 'AbortError');
    const source = context.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(context, 'hugagent-voice-capture', { numberOfInputs: 1, numberOfOutputs: 0, channelCount: 1, channelCountMode: 'explicit' });
    this.node = node;
    node.port.onmessage = (event: MessageEvent<Float32Array>) => {
      if (this.cancelled) return;
      if (this.samples >= context.sampleRate * MAX_VOICE_SECONDS) return;
      this.chunks.push(event.data);
      this.samples += event.data.length;
    };
    source.connect(node);
    this.timer = setTimeout(onLimit, MAX_VOICE_SECONDS * 1000);
    return stream;
  }

  stop(): Blob {
    const rate = this.context?.sampleRate || 48000;
    const chunks = this.chunks;
    this.close();
    return encodeVoiceWav(chunks, rate);
  }

  close() {
    this.cancelled = true;
    clearTimeout(this.timer);
    this.node?.disconnect();
    if (this.node) this.node.port.onmessage = null;
    this.node = null;
    this.stream?.getTracks().forEach(track => { track.onended = null; track.stop(); });
    this.stream = null;
    void this.context?.close().catch(() => {});
    this.context = null;
    this.chunks = [];
  }
}
