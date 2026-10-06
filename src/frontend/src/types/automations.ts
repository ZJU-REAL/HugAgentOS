import type { EditionResourceFields } from '../editionModelTypes';

// ── My Space types ─────────────────────────────────────────────────────────

export type MySpaceTab = 'assets' | 'kb' | 'favorites' | 'shares' | 'notifications';

/** 知识库分档：公共库 / 私有库 */
export type KbTabKey = 'public' | 'private';

// ── Automation types ────────────────────────────────────────────
export type AutomationTaskType = 'prompt' | 'plan';

export type AutomationStatus = 'active' | 'paused' | 'disabled' | 'completed' | 'expired';

export type AutomationRunStatus = 'running' | 'success' | 'failed';

export type AutomationScheduleType = 'recurring' | 'once' | 'manual';

export interface AutomationTask {
  task_id: string;
  execution_location?: 'local' | 'cloud';
  device_id?: string;
  device_name?: string;
  project_id?: string;
  project_name?: string;
  project_local_path?: string;
  task_type: AutomationTaskType;
  prompt?: string;
  plan_id?: string;
  plan_title?: string;
  cron_expression: string;
  schedule_type: AutomationScheduleType;
  timezone: string;
  name?: string;
  description?: string;
  status: AutomationStatus;
  next_run_at?: string;
  last_run_at?: string;
  run_count: number;
  max_runs?: number;
  consecutive_failures: number;
  max_failures: number;
  last_error?: string;
  enabled_mcp_ids: string[];
  enabled_skill_ids: string[];
  enabled_kb_ids: string[];
  enabled_agent_ids: string[];
  sidebar_activated?: boolean;
  /** Channel delivery target (echoed back when editing): absent means in-app only */
  channel_id?: string | null;
  conversation_id?: string | null;
  created_at: string;
  updated_at: string;
}

export interface AutomationChatGroup {
  taskId: string;
  taskName: string;
  runs: AutomationRun[];
}

export interface AutomationRun {
  run_id: string;
  task_id: string;
  status: AutomationRunStatus;
  chat_id?: string;
  result_summary?: string;
  error_message?: string;
  started_at: string;
  completed_at?: string;
  duration_ms?: number;
  usage?: Record<string, unknown>;
}

export interface AutomationNotification {
  id: string;
  task_id: string;
  task_name: string;
  status: 'success' | 'failed';
  summary: string;
  chat_id?: string;
  timestamp: number;
  read: boolean;
}

export interface ResourceItem extends EditionResourceFields {
  origin?: 'local' | 'cloud';
  chat_id?: string;
  project_id?: string;
  id: string;
  type: 'document' | 'image' | 'favorite';
  name: string;
  mime_type?: string;
  file_id?: string;
  download_url?: string;
  size?: number;
  source_kind?: 'user_upload' | 'ai_generated';
  knowledge_base_count?: number;
  knowledge_bases?: Array<{ kb_id: string; name: string }>;
  source_chat_id?: string;
  source_chat_title?: string;
  content_preview?: string;
  created_at: string;
  // Folder membership (only meaningful for the assets type)
  user_folder_id?: string | null;
}

/** Personal folder node (the tree structure under "MySpace"). */
export interface PersonalFolderNode {
  folder_id: string;
  parent_folder_id: string | null;
  name: string;
  created_at?: string | null;
  children: PersonalFolderNode[];
}
