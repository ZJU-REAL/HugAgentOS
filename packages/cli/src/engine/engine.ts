/**
 * The one contract between the TUI / output modes and where the agent runs.
 *
 * Every mode (local, cloud, device:<id>) speaks the same HugAgentOS HTTP + SSE API,
 * so events reach the UI unchanged: see
 * .claude/skills/hugagent-frontend-dev/references/sse-events.md for every event type.
 * The UI depends on this file only, never on a concrete engine.
 */
import type { ChatEvent, ChatHistoryMessage, FileDecision, QuestionAnswer } from '@hugagent/chat-sdk';

export type { ChatEvent, ChatHistoryMessage, FileDecision, QuestionAnswer };

/** Where the agent runs. `local` works in the current directory on this machine. */
export type Mode = 'local' | 'cloud' | `device:${string}`;

export interface EngineInfo {
  mode: Mode;
  /** Short status-bar text, e.g. "local · ~/repo" or "cloud · agent.example.com". */
  label: string;
  /** Base URL the engine talks to (local: loopback engine). */
  server: string;
  /** Absolute directory the agent works in; set only when it can reach this machine's files. */
  workspace?: string;
  /** The project new sessions are created in (local: the project bound to `workspace`). */
  projectId?: string;
  /** False for cloud and device modes: the agent cannot read or change local files. */
  canAccessCwd: boolean;
}

export interface SessionSummary {
  chatId: string;
  title: string;
  updatedAt?: string;
  projectId?: string;
}

export interface ModelOption {
  providerId: string;
  modelName: string;
  label: string;
  current?: boolean;
}

/** steer: apply inside the running turn; followUp: right after it; nextRun: as the next message. */
export type DeliveryMode = 'steer' | 'followUp' | 'nextRun';

/** Tool execution preset, as served by /v1/tool-approval. */
export type PermissionMode = 'ask' | 'auto' | 'full';

export interface SendOptions {
  clientSessionId?: string;
  /** Per-message model override (ChatRequest.model_provider_id / model_name). */
  model?: { providerId: string; modelName: string };
  attachments?: { file_id: string; name: string; mime_type: string }[];
  signal?: AbortSignal;
}

/** What a manual compaction did; `compacted` is false when nothing was old enough to fold. */
export interface CompactSummary {
  compacted: boolean;
  tokensBefore: number | null;
  tokensAfter: number | null;
}

export interface ActiveRun {
  runId: string;
  /** Last delivered event offset, for resume(). */
  offset?: number;
}

export interface Engine {
  readonly info: EngineInfo;

  /** Start or attach to the backend and bind the workspace. Idempotent. */
  connect(): Promise<void>;
  /** Release the connection; never stops a local engine other clients may share. */
  close(): Promise<void>;

  newSession(title?: string): Promise<SessionSummary>;
  /** Newest first; local mode lists only the sessions of the bound project. */
  listSessions(limit?: number): Promise<SessionSummary[]>;
  /** Oldest first, at most `limit` latest messages. */
  history(chatId: string, limit?: number): Promise<ChatHistoryMessage[]>;
  /** Branch a session (POST /v1/chats/{id}/fork); returns the new session. */
  fork(chatId: string, messageId?: string): Promise<SessionSummary>;

  /** Start a turn and stream its events until the run ends (`end` / [DONE]). */
  send(chatId: string, message: string, options?: SendOptions): AsyncIterable<ChatEvent>;
  /** Re-follow a run from an event offset (reconnect, or after /resume). */
  resume(runId: string, fromOffset?: number, signal?: AbortSignal): AsyncIterable<ChatEvent>;
  activeRun(chatId: string): Promise<ActiveRun | null>;
  cancel(runId: string): Promise<void>;
  steer(runId: string, message: string, mode?: DeliveryMode): Promise<void>;

  /** Answer a `file_confirm` event (file writes, commands, tool permissions all use it). */
  confirm(chatId: string, confirmId: string, decision: FileDecision, optionId?: string): Promise<void>;
  /** Answer a `user_question` event. */
  answer(chatId: string, requestId: string, answers: QuestionAnswer[]): Promise<void>;

  /** Compact a session's context now (POST /v1/chats/{id}/compact). */
  compact(chatId: string): Promise<CompactSummary>;

  listModels(): Promise<ModelOption[]>;
  getPermissionMode(): Promise<PermissionMode>;
  setPermissionMode(mode: PermissionMode): Promise<void>;
}

/** Builds the engine for a mode; implemented in engine/index.ts. */
export type EngineFactory = (mode: Mode, options: { cwd: string }) => Promise<Engine>;

/** Options every output mode receives from main.ts. */
export interface RunOptions {
  engine: Engine;
  /** Create engines for /mode switches. */
  createEngine: EngineFactory;
  cwd: string;
  /** Session to continue (-c / -r / --session). */
  chatId?: string;
  model?: { providerId: string; modelName: string };
  /** `hugagent [prompt]`: send this as the first message once the UI is up. */
  prompt?: string;
  /** `-r` without an id: let the user pick a session of this engine to resume. */
  pickSession?: boolean;
}
