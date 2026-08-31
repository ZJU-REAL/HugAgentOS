import { create } from 'zustand';
import { getMainModelCapabilities, type ModelCapabilities } from '../api';
import { writeLocal, removeLocal } from '../storage';

const SELECTED_MODEL_PROVIDER_KEY = 'hugagent_selected_model_provider_id';

function loadSelectedProviderId(): string | null {
  if (typeof window === 'undefined') return null;
  return window.localStorage.getItem(SELECTED_MODEL_PROVIDER_KEY);
}

function saveSelectedProviderId(providerId: string | null): void {
  if (typeof window === 'undefined') return;
  if (providerId) writeLocal(SELECTED_MODEL_PROVIDER_KEY, providerId);
  else removeLocal(SELECTED_MODEL_PROVIDER_KEY);
}

interface ModelCapabilitiesState {
  capabilities: ModelCapabilities;
  selectedModelProviderId: string | null;
  loaded: boolean;
  fetching: boolean;
  setSelectedModelProviderId: (providerId: string | null) => void;
  fetchCapabilities: () => Promise<void>;
}

export const useModelCapabilitiesStore = create<ModelCapabilitiesState>((set, get) => ({
  capabilities: {
    supports_reasoning_effort: false,
    user_model_switch_enabled: false,
    user_selectable_models: [],
    // 读图能力默认按「不能」起步，接口回来后再放开：宁可先不许诺，也不要在
    // 未配置视觉模型的部署上让用户以为传图有用。
    can_read_image: false,
    vision_bridge_enabled: false,
  },
  selectedModelProviderId: loadSelectedProviderId(),
  loaded: false,
  fetching: false,
  setSelectedModelProviderId: (providerId) => {
    saveSelectedProviderId(providerId);
    set({ selectedModelProviderId: providerId });
  },
  fetchCapabilities: async () => {
    if (get().fetching) return;
    set({ fetching: true });
    try {
      const caps = await getMainModelCapabilities();
      const current = get().selectedModelProviderId;
      const defaultModel = caps.user_selectable_models.find((m) => m.is_default)
        || caps.user_selectable_models[0]
        || null;
      const stillAvailable = current
        ? caps.user_selectable_models.some((m) => m.provider_id === current)
        : false;
      const nextSelected = caps.user_model_switch_enabled
        ? (stillAvailable ? current : defaultModel?.provider_id || null)
        : null;
      if (nextSelected !== current) saveSelectedProviderId(nextSelected);
      set({
        capabilities: caps,
        selectedModelProviderId: nextSelected,
        loaded: true,
      });
    } catch {
      // Fail silently: keep the old value or default to false; the frontend only loses the "high/ultra-high" two options
    } finally {
      set({ fetching: false });
    }
  },
}));
