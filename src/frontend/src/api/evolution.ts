import type { EvolutionSummary } from '../types';
import { apiRequest, unwrapData } from './core';

// ── Evolution: per-turn settlement (GCE ticket 06) ───────────────────────────
//
// The SSE push is the fast path for a live client; this is the durable one —
// used after a reload, or when the connection dropped before settlement
// finished. A turn still settling legitimately reports `pending`.
export async function getTurnSettlement(messageId: string): Promise<EvolutionSummary> {
  const wrapped = await apiRequest<unknown>(
    `/v1/evolution/turns/${encodeURIComponent(messageId)}`,
  );
  return unwrapData<EvolutionSummary>(wrapped);
}

// ── Evolution: contribution summary (settings panel detail card) ─────────────
//
// The separate contribution setting is gone from the UI: participation follows
// the single evolution switch in EvolutionPrefs. Only the summary — "what did
// my conversations actually produce" — remains its own endpoint.
export interface EvolutionContributionItem {
  candidate_id: string;
  target_kind: 'memory' | 'skill' | 'workflow' | 'ontology' | 'prompt';
  summary: string;
  status: string;
  risk_tier: string;
  your_episodes: number;
  total_evidence: number;
  created_at?: string | null;
}

export interface EvolutionContributions {
  episodes: number;
  contributed_episodes: number;
  private_episodes: number;
  memory_written: number;
  candidates: EvolutionContributionItem[];
}

export async function getEvolutionContributions(): Promise<EvolutionContributions> {
  return unwrapData<EvolutionContributions>(await apiRequest<unknown>('/v1/evolution/contributions'));
}

// ── Evolution: personal candidate approval (settings console) ────────────────
/** The concrete content of a proposed change — what approving it will do. */
type EvolutionChangePreview =
  | {
      type: 'skill_document';
      display_name: string;
      description: string;
      allowed_tools: string[];
      content: string;
    }
  | {
      type: 'skill_sequence';
      display_name: string;
      description: string;
      allowed_tools: string[];
      steps: string[];
      ordering_constraints: Array<Record<string, unknown>>;
    }
  | {
      type: 'memory_ops';
      operations: Array<{
        operation: string;
        text: string;
        reason: string;
        before?: unknown;
        after?: unknown;
      }>;
    }
  | Record<string, never>;

export interface MyEvolutionCandidate {
  candidate_id: string;
  target_kind: string;
  operation: string;
  summary: string;
  status: string;
  risk_tier: string;
  your_episodes: number;
  total_evidence: number;
  own_share: number;
  tool_sequence: string[];
  /** What the button does. Rows the user cannot action are not returned at all. */
  action: string;
  action_label: string;
  action_effect: string;
  change: EvolutionChangePreview;
  created_at?: string | null;
}

export async function getMyEvolutionCandidates(): Promise<{
  candidates: MyEvolutionCandidate[];
  own_evidence_threshold: number;
}> {
  return unwrapData(await apiRequest<unknown>('/v1/evolution/my-candidates'));
}

export async function approveMyEvolutionCandidate(candidateId: string): Promise<{
  skill_id: string;
  scope: string;
  tools: string[];
}> {
  return unwrapData(
    await apiRequest<unknown>(
      `/v1/evolution/my-candidates/${encodeURIComponent(candidateId)}/approve`,
      { method: 'POST' },
    ),
  );
}

// ── Evolution: per-user preferences ─────────────────────────────────────────
export interface EvolutionPrefs {
  enabled: boolean;
  /** `none` is a first-class choice, not a degraded state. */
  ontology_mode: 'none' | 'any' | 'specific';
  ontology_pack_id: string;
  mechanisms: string[];
  min_support: number;
  auto_approve_low_risk: boolean;
}

export interface OntologyPackOption {
  pack_id: string;
  name: string;
  active: boolean;
}

export async function getEvolutionPrefs(): Promise<{
  prefs: EvolutionPrefs;
  ontology_packs: OntologyPackOption[];
  ontology_modes: string[];
}> {
  return unwrapData(await apiRequest<unknown>('/v1/evolution/prefs'));
}

export async function updateEvolutionPrefs(
  patch: Partial<EvolutionPrefs>,
): Promise<EvolutionPrefs> {
  return unwrapData(
    await apiRequest<unknown>('/v1/evolution/prefs', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }),
  );
}
