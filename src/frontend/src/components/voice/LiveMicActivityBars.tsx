// Adapted from Caho1/open-claude-ui/code-examples/LiveMicActivityBars.tsx.
// MIT, Copyright (c) 2026 Caho1. Full notice: LICENSE.open-claude-ui.
import { useEffect, useRef } from 'react';

const BANDS = [[1, 3], [3, 6], [6, 12], [12, 22], [22, 36], [36, 56]];
const WEIGHTS = [1.1, 1.2, 1.3, 1.2, 1, 0.9];
export function LiveMicActivityBars({ stream, waiting }: { stream: MediaStream | null; waiting: boolean }) {
  const bars = useRef<Array<SVGRectElement | null>>([]);
  useEffect(() => {
    let context: AudioContext | undefined;
    let analyser: AnalyserNode | undefined;
    let source: MediaStreamAudioSourceNode | undefined;
    let frame = 0;
    if (stream) {
      try {
        context = new AudioContext();
        void context.resume().catch(() => {});
        analyser = context.createAnalyser(); analyser.fftSize = 512;
        analyser.smoothingTimeConstant = 0.5; analyser.minDecibels = -70; analyser.maxDecibels = -10;
        source = context.createMediaStreamSource(stream); source.connect(analyser);
      } catch { /* Recording remains usable if visualization is unavailable. */ }
    }
    const data = new Uint8Array(256);
    const started = performance.now();
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const draw = () => {
      analyser?.getByteFrequencyData(data);
      const phase = ((performance.now() - started) % 1800) / 1800;
      bars.current.forEach((bar, i) => {
        if (!bar) return;
        let height = 7, opacity = 1;
        if (waiting && !reduced) {
          const start = i * 0.15;
          opacity = phase < 0.6 ? phase < start ? 0.35 : phase < start + 0.2 ? 0.35 + 0.65 * (phase - start) / 0.2 : 1 : 1 - 0.65 * (phase - 0.6) / 0.4;
        } else if (analyser) {
          // The reference bin ranges assume 44.1kHz; scale them for the real device.
          const scale = 44100 / (context?.sampleRate || 44100);
          const start = Math.max(1, Math.round(BANDS[i][0] * scale));
          const end = Math.min(data.length, Math.max(start + 1, Math.round(BANDS[i][1] * scale)));
          let sum = 0;
          for (let j = start; j < end; j++) sum += data[j];
          const normalized = sum / Math.max(1, end - start) / 255;
          const gated = Math.max(0, normalized - 0.08) / 0.92;
          height = 4 + 20 * Math.min(1, Math.pow(gated, 0.7) * 1.5 * WEIGHTS[i]);
        }
        bar.setAttribute('height', String(height)); bar.setAttribute('y', String((32 - height) / 2));
        bar.setAttribute('opacity', String(Math.max(0.2, Math.min(1, opacity))));
      });
      frame = requestAnimationFrame(draw);
    };
    frame = requestAnimationFrame(draw);
    return () => { cancelAnimationFrame(frame); source?.disconnect(); void context?.close().catch(() => {}); };
  }, [stream, waiting]);
  return <svg width="48" height="32" viewBox="0 0 48 32" fill="currentColor" aria-hidden="true">
    {[11, 16, 21, 26, 31, 36].map((x, i) => <rect key={x} ref={node => { bars.current[i] = node; }} x={x} y={14} width={3} height={4} rx={1.5} />)}
  </svg>;
}
