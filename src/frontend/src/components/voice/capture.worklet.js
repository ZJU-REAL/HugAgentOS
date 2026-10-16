// Microphone samples only; analysis/playback never connects to speakers.
class VoiceCapture extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0]?.[0];
    if (channel) this.port.postMessage(channel.slice());
    return true;
  }
}
registerProcessor('hugagent-voice-capture', VoiceCapture);
