/**
 * Payload parsers for the backend SSE events the TUI acts on
 * (.claude/skills/hugagent-frontend-dev/references/sse-events.md). Lenient: missing fields
 * fall back to empty values, unknown shapes return null.
 */
import { str } from './format.js';

export interface ConfirmOption { id: string; title: string; brief?: string }

/** A `file_confirm` (or `design_pick`) waiting for the user. */
export interface ConfirmRequest {
  confirmId: string;
  op: string;
  path: string;
  message?: string;
  kind?: string;
  options: ConfirmOption[];
  /** design_pick: pick one option or let the assistant decide; no allow/deny. */
  pickOnly: boolean;
}

export interface QuestionOption { id: string; label: string; description?: string; recommended: boolean }

export interface Question {
  id: string;
  header?: string;
  question: string;
  description?: string;
  multiSelect: boolean;
  options: QuestionOption[];
}

export interface QuestionRequest { requestId: string; questions: Question[] }

export type PlanStatus = 'pending' | 'in_progress' | 'completed';
export interface Plan { title: string; steps: { title: string; status: PlanStatus }[] }

export interface QueuedRunHandoff { runId: string; message: string; deliveryMode: string }

const RECOMMENDED_SUFFIX = /\s*(?:\((?:recommended|推荐)\)|（(?:recommended|推荐)）)\s*$/i;

export function parseConfirm(ev: Record<string, unknown>): ConfirmRequest | null {
  const confirmId = str(ev.confirm_id);
  if (!confirmId) return null;
  const rawOptions = Array.isArray(ev.options) ? (ev.options as Record<string, unknown>[]) : [];
  return {
    confirmId,
    op: str(ev.op),
    path: str(ev.logical_path ?? ev.path),
    message: str(ev.message ?? ev.question) || undefined,
    kind: str(ev.kind) || undefined,
    options: rawOptions
      .map((o) => ({ id: str(o.id ?? o.option_id), title: str(o.title ?? o.label), brief: str(o.brief ?? o.description) || undefined }))
      .filter((o) => o.id && o.title),
    pickOnly: ev.type === 'design_pick' || ev.kind === 'design_pick',
  };
}

export function parseQuestion(ev: Record<string, unknown>): QuestionRequest | null {
  const requestId = str(ev.request_id);
  const rawQuestions = Array.isArray(ev.questions) ? (ev.questions as Record<string, unknown>[]) : [];
  const questions = rawQuestions
    .map((q) => ({
      id: str(q.id),
      header: str(q.header) || undefined,
      question: str(q.question),
      description: str(q.description) || undefined,
      multiSelect: q.multi_select === true,
      options: (Array.isArray(q.options) ? (q.options as Record<string, unknown>[]) : [])
        .map((o) => {
          const label = str(o.label);
          return {
            id: str(o.id),
            label: label.replace(RECOMMENDED_SUFFIX, ''),
            description: str(o.description) || undefined,
            recommended: o.recommended === true || RECOMMENDED_SUFFIX.test(label),
          };
        })
        .filter((o) => o.id && o.label),
    }))
    .filter((q) => q.id && q.question);
  return requestId && questions.length ? { requestId, questions } : null;
}

export function parsePlan(ev: Record<string, unknown>): Plan | null {
  const steps = (Array.isArray(ev.steps) ? (ev.steps as Record<string, unknown>[]) : [])
    .map((s) => ({
      title: str(s?.title).trim(),
      status: (s?.status === 'in_progress' || s?.status === 'completed' ? s.status : 'pending') as PlanStatus,
    }))
    .filter((s) => s.title);
  return steps.length ? { title: str(ev.title), steps } : null;
}

export function parseQueuedRun(ev: Record<string, unknown>): QueuedRunHandoff | null {
  const runId = str(ev.run_id);
  return runId ? { runId, message: str(ev.message), deliveryMode: str(ev.delivery_mode) } : null;
}

export function errorText(value: unknown): string {
  if (!value) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'object') {
    const v = value as Record<string, unknown>;
    return str(v.message ?? v.detail ?? v.error) || JSON.stringify(value);
  }
  return String(value);
}
