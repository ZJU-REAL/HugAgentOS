import { create } from 'zustand';

interface CommunityEditionState {
  edition: 'ce';
  mode: 'ce';
  features: Record<string, boolean>;
  loaded: true;
  fetching: false;
  fetchEdition: () => Promise<void>;
}

export const useEditionStore = create<CommunityEditionState>(() => ({
  edition: 'ce',
  mode: 'ce',
  features: {},
  loaded: true,
  fetching: false,
  fetchEdition: async () => undefined,
}));
