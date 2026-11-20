import { useEffect, useState } from 'react';

import type { MarketPickItem } from './MarketPickerModal';
import { t } from '../../i18n';

/** Data helpers shared by the two chat-mode editors (see ChatModeFormFields). */

export type ChatModeScope = 'private' | 'official';
export type ChatModeEffort = 'fast' | 'medium' | 'high' | 'max';
export type ChatModeOption = { value: string; label: string };

interface CapabilityItem { id: string; name?: string; description?: string; disabled_globally?: boolean }
export interface ChatModeCapabilityOptions {
  mcp?: CapabilityItem[];
  skills?: CapabilityItem[];
  plugins?: CapabilityItem[];
  agents?: CapabilityItem[];
  market_skills?: CapabilityItem[];
  market_agents?: CapabilityItem[];
  market_plugins?: CapabilityItem[];
  prompt_kinds?: Array<{ key: string; label: string; builtin: boolean }>;
}

export const CHAT_MODE_EFFORT_OPTIONS: ChatModeOption[] = [
  { value: 'fast', label: '快速（不思考）' },
  { value: 'medium', label: '思考·中' },
  { value: 'high', label: '思考·高' },
  { value: 'max', label: '思考·超高' },
];

export function chatModeEffortLabel(effort: string): string {
  const label = CHAT_MODE_EFFORT_OPTIONS.find((o) => o.value === effort)?.label;
  return label ? t(label) : effort;
}

/** Defaults for a new mode; each editor adds its own extra fields. */
export const CHAT_MODE_DEFAULTS = {
  name: '',
  slug: '',
  description: '',
  enabled: true,
  tool_scope: 'restricted',
  mcp_server_ids: [] as string[],
  skill_ids: [] as string[],
  plugin_ids: [] as string[],
  agent_ids: [] as string[],
  manual_invoke_enabled: true,
  code_exec_enabled: false,
  max_iters: null as number | null,
  default_effort: 'fast' as ChatModeEffort,
  effort_locked: false,
  prompt_text: '',
};

// A globally disabled capability can still be picked into a mode (the mode is an
// explicit, narrow grant), but it is marked so it doesn't look like a mistake.
const toOption = (r: CapabilityItem): ChatModeOption => ({
  value: r.id,
  label: `${r.name || r.id}${r.disabled_globally ? t('（全局已停用）') : ''}`,
});
const toPick = (i: CapabilityItem): MarketPickItem => ({ id: i.id, name: i.name, description: i.description });

export interface ChatModeCapabilities {
  mcp: ChatModeOption[];
  skills: ChatModeOption[];
  plugins: ChatModeOption[];
  agents: ChatModeOption[];
  marketSkills: MarketPickItem[];
  marketAgents: MarketPickItem[];
  marketPlugins: MarketPickItem[];
  promptKinds: ChatModeOption[];
}

const EMPTY_CAPABILITIES: ChatModeCapabilities = {
  mcp: [], skills: [], plugins: [], agents: [],
  marketSkills: [], marketAgents: [], marketPlugins: [], promptKinds: [],
};

/**
 * Loads the mode's own capability list: everything, not filtered by on/off state
 * (picking a capability you switched off means "use it only in this mode"), plus
 * market items that aren't installed yet (installed on save). Market items stay
 * out of the dropdowns — hundreds of them don't scroll or search well — and are
 * picked in a modal instead. On failure the selects fall back to free tag input.
 */
export function useChatModeCapabilities(
  load: () => Promise<ChatModeCapabilityOptions>,
  deps: readonly unknown[],
): ChatModeCapabilities {
  const [caps, setCaps] = useState<ChatModeCapabilities>(EMPTY_CAPABILITIES);
  useEffect(() => {
    let live = true;
    load().then((d) => {
      if (!live) return;
      setCaps({
        mcp: (d.mcp || []).map(toOption),
        skills: (d.skills || []).map(toOption),
        plugins: (d.plugins || []).map(toOption),
        agents: (d.agents || []).map(toOption),
        marketSkills: (d.market_skills || []).map(toPick),
        marketAgents: (d.market_agents || []).map(toPick),
        marketPlugins: (d.market_plugins || []).map(toPick),
        promptKinds: (d.prompt_kinds || []).map((k) => ({ value: k.key, label: k.label })),
      });
    }).catch(() => undefined);
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- callers pass the load inputs as deps
  }, deps);
  return caps;
}
