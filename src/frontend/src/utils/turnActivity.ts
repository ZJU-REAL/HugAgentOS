import type { ShellStep } from '../components/tool/ToolRunShell';
import type { AgentMarkState } from '../components/common/AgentMark';
import { computeEffectiveStatus } from '../components/tool/renderers/utils';
import { t } from '../i18n';

/**
 * Pacing of the "what is the agent doing right now" signal while a reply
 * streams. Every wait has a visible owner: the run that is still in flight, or
 * the tail row under the text — never a silent gap that reads as a hang. A run
 * with tools shows its phase through the lit tool-kind chip and the title scan;
 * the agent mark (and `agentStateForPhase`) only appears on tool-less rows.
 */

/** Two tool calls this close apart are one hand-over: keep the previous label, don't flash "done". */
export const HANDOFF_MS = 400;
/** Text that has paused this long gets a "working out the next step" line under it. */
export const TAIL_WAIT_MS = 600;
/** Waits longer than this show their own clock. */
export const WAIT_CLOCK_MS = 5_000;
/** Waits longer than this say so plainly. */
export const SLOW_WAIT_MS = 8_000;
/** The idle steps a reply's own render depends on (the wait clock ticks in its own row). */
export const ACTIVITY_THRESHOLDS = [HANDOFF_MS, TAIL_WAIT_MS, SLOW_WAIT_MS] as const;

/**
 * - `tool` / `compaction` — something is executing
 * - `thinking` — reasoning is the latest step of the run
 * - `handoff` — a tool just finished and the next one is probably coming
 * - `gap` — tools finished, the model has the results and has not answered yet
 * - `done` — the reply moved past this run (text came, or the stream ended)
 */
export type RunPhase = 'tool' | 'compaction' | 'thinking' | 'handoff' | 'gap' | 'done';

interface RunPhaseInput {
  isStreaming: boolean;
  /**
   * The run is the last thing in the reply. Undefined for callers that do not
   * track the tail (Canvas transcripts, standalone shells): they keep the old
   * rule where only an active thinking step holds the run open.
   */
  isTail?: boolean;
  /** Ms since the reply last showed any activity. */
  idleMs: number;
  /** Callers that already resolved each tool's effective status pass the verdict in. */
  anyToolRunning?: boolean;
}

export function runPhase(steps: ShellStep[], { isStreaming, isTail, idleMs, anyToolRunning }: RunPhaseInput): RunPhase {
  if (steps.some((step) => step.kind === 'compaction' && step.progress.status === 'running')) return 'compaction';
  if (anyToolRunning ?? steps.some((step) => step.kind === 'tool' && computeEffectiveStatus(step.tool, isStreaming) === 'running')) return 'tool';
  if (!isStreaming) return 'done';
  if (isTail === undefined) {
    return steps.some((step) => step.kind === 'thinking' && step.active) ? 'thinking' : 'done';
  }
  if (!isTail) return 'done';
  if (steps[steps.length - 1]?.kind === 'thinking') return 'thinking';
  return idleMs < HANDOFF_MS ? 'handoff' : 'gap';
}

export function agentStateForPhase(phase: RunPhase): AgentMarkState {
  if (phase === 'thinking' || phase === 'gap') return 'think';
  if (phase === 'done') return 'rest';
  return 'work';
}

/** A wait label that admits it once the wait has gone on for a while. */
export function waitLabel(idleMs: number, label: string): string {
  return idleMs >= SLOW_WAIT_MS ? t('仍在等待模型响应…') : label;
}
