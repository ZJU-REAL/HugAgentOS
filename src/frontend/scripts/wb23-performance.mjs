import assert from 'node:assert/strict';
import { cpus, totalmem, platform, arch } from 'node:os';

/**
 * Install before real browser interactions. Event Timing measures input processing
 * through the next paint; it does not wait for network or third-party content.
 * No route interception or host store injection is performed here.
 */
export async function installInteractionRecorder(page) {
  await page.evaluate(() => {
    if (!PerformanceObserver.supportedEntryTypes.includes('event')) {
      throw new Error('Browser does not support Event Timing');
    }
    window.__wb23Timing = { entries: [], inputs: [], observer: null };
    for (const name of ["click", "keydown"]) {
      document.addEventListener(name, event => {
        if (event.isTrusted) window.__wb23Timing.inputs.push({ name, time: performance.now() });
      }, { capture: true });
    }
    const observer = new PerformanceObserver(list => {
      for (const entry of list.getEntries()) {
        if (entry.interactionId && ['click', 'keydown'].includes(entry.name)) {
          window.__wb23Timing.entries.push({
            interactionId: entry.interactionId,
            name: entry.name, startTime: entry.startTime, duration: entry.duration,
          });
        }
      }
    });
    observer.observe({ type: 'event', buffered: false, durationThreshold: 16 });
    window.__wb23Timing.observer = observer;
  });
}

/**
 * Record one actual user operation and its UI feedback. All three operation
 * classes must be represented. Fast events absent from Event Timing are recorded
 * conservatively at the 16ms observation threshold, never as zero.
 * ready is a DOM locator which proves the operation produced its expected feedback.
 */
export async function sampleInteraction(page, operation, action, ready) {
  assert.ok(['pin', 'sort', 'more'].includes(operation), 'known A15 operation');
  const start = await page.evaluate(() => performance.now());
  await action();
  await ready.waitFor({ state: 'visible' });
  // Give the browser observer time to deliver the input/paint entry.
  await page.waitForTimeout(120);
  const observed = await page.evaluate(startTime => ({
    entries: window.__wb23Timing.entries.filter(entry => entry.startTime >= startTime),
    inputs: window.__wb23Timing.inputs.filter(entry => entry.time >= startTime),
  }), start);
  assert.ok(observed.inputs.length >= 1 && observed.inputs.length <= 2,
    "one real trusted click or key operation must be observed");
  const entries = observed.entries;
  const unique = [...new Map(entries.map(entry => [entry.interactionId, entry])).values()];
  assert.ok(unique.length <= 1, 'one measured click/key interaction per operation');
  return { operation, durationMs: unique[0]?.duration ?? 16,
    precision: unique.length ? 'browser-event-timing-8ms' : 'below-16ms-observation-threshold' };
}

export function performanceSummary(samples, browserVersion, buildMode) {
  assert.ok(samples.length >= 100, 'A15 needs at least 100 measured operations');
  for (const operation of ['pin', 'sort', 'more']) {
    assert.ok(samples.some(sample => sample.operation === operation), operation + ' sampled');
  }
  assert.ok(samples.every(sample => Number.isFinite(sample.durationMs) && sample.durationMs >= 0));
  assert.ok(browserVersion && ['production', 'development'].includes(buildMode));
  const durations = samples.map(sample => sample.durationMs).sort((a, b) => a - b);
  const p95Ms = durations[Math.ceil(durations.length * 0.95) - 1];
  return {
    sampleCount: samples.length, samples, p95Ms, targetMs: 100, passed: p95Ms <= 100,
    cpu: cpus()[0]?.model ?? 'unknown', logicalCpus: cpus().length,
    memoryBytes: totalmem(), platform: platform(), architecture: arch(),
    browserVersion, buildMode,
    measurement: 'input-to-next-paint; excludes later HTTP and third-party loading; 8ms browser precision',
    instanceIsolation: 'requires separate real HTTP resource and iframe assertions',
  };
}
