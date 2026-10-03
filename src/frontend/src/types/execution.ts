import type { ToolCall } from './messages';
import type { CitationItem } from './workspace';

// ── Plan Mode types ───────────────────────────────────────────────────────

export type PlanStatus = 'draft' | 'approved' | 'running' | 'completed' | 'failed' | 'cancelled';

export type PlanStepStatus = 'pending' | 'running' | 'success' | 'failed' | 'skipped';

export interface PlanStep {
  step_id: string;
  step_order: number;
  title: string;
  description: string;
  expected_tools: string[];
  expected_skills: string[];
  expected_agents: string[];
  status: PlanStepStatus;
  result_summary?: string;
  tool_calls?: ToolCall[];
  ai_output?: string;
  error_message?: string;
  started_at?: string;
  completed_at?: string;
}

export interface Plan {
  plan_id: string;
  title: string;
  description: string;
  task_input: string;
  status: PlanStatus;
  total_steps: number;
  completed_steps: number;
  result_summary?: string;
  steps: PlanStep[];
  created_at: string;
  updated_at: string;
}

/* ───── 批量作业（工作流模式）───── */

/** 台账聚合：分母是 total，**别用 done 当分母**——查无/失败也是结算掉的。 */
export interface JobStats {
  total: number;
  done: number;
  pending: number;
  failed: number;
  not_found: number;
  needs_review: number;
  running: number;
  settled: number;
  remaining: number;
}

export interface JobBrief {
  job_id: string;
  chat_id: string;
  name: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted';
  stats: JobStats;
  usage: { calls?: number; tokens?: number };
  budget_left: { calls_left?: number; tokens_left?: number; seconds_left?: number };
  error: string;
  created_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
}

// ── Batch execution feature ────────────────────────────────────────────────

export type BatchSourceType = 'xlsx' | 'word_files' | 'text_list';

export interface BatchPlanMeta {
  plan_id: string;
  total: number;
  source_type: BatchSourceType;
  preview: Record<string, unknown>[];
  default_template: string;
  placeholder_keys: string[];
  chat_id?: string;
  warnings?: string[];   // truncation / cap warnings surfaced from backend
}

export interface BatchItemResult {
  index: number;
  total?: number;
  status: 'success' | 'skipped';
  content?: string;
  error?: string;
  retry_count: number;
  progress?: { done: number; success: number; failed: number };
  // Optional captured side-channels from the per-item sub-agent run.
  // Lets the frontend reuse the chat-bubble's tool-call / artifact /
  // citation primitives for diverse output formats (charts, Word/Excel
  // exports, KB-cited research) without bespoke rendering.
  tool_calls?: ToolCall[];
  artifacts?: unknown[];
  citations?: CitationItem[];
}

export interface BatchPlanState {
  meta: BatchPlanMeta;
  template?: string;        // user-edited template (after confirm)
  status: 'awaiting_confirm' | 'running' | 'done' | 'cancelled' | 'error';
  results: BatchItemResult[];
  startedAt?: number;
  finishedAt?: number;
  errorMsg?: string;
  summary?: { total: number; success: number; failed: number };
}

// ── Autonomous Loop (long-running autonomous operation) ─────────────────────
export interface LoopGoalSpec {
  objective: string;
  /** Acceptance criteria (optional; if left empty the backend extracts them from objective).
   *  Judgment is done by a read-only reviewer sub-agent verifying the actual output itself;
   *  there are no more scripted verification fields such as verify_cmd / numeric scores /
   *  thresholds (removed wholesale). */
  acceptance_criteria?: string[];
}

export interface LoopBudget {
  max_iters: number;
  max_wall_clock_s: number;
  max_tokens: number;
}

export interface LoopItem {
  loop_id: string;
  title: string;
  status: string;
  goal_spec: LoopGoalSpec;
  budget: LoopBudget;
  iteration_count: number;
  tokens_spent: number;
  final_score: number | null;
  result_summary?: string | null;
  chat_id?: string | null;
  created_at?: string | null;
}

export interface LoopIterationItem {
  seq: number;
  verdict: string;
  score: number | null;
  reasoning?: string;
  tool_calls: number;
  tokens: number;
  decided_by?: string;
}
