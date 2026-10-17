import { create } from 'zustand';
import { personalRequest, type PersonalAgent } from '../api';
import { useAuthStore } from '../../../stores/authStore';
import { DEFAULT_AGENT_NAME } from './avatarPresets';

interface AgentNameState {
  owner?: string;
  name: string;
  loaded: boolean;
  setName: (name?: string) => void;
  load: () => Promise<void>;
  /** Read the name again, e.g. after IDENTITY.md was edited by hand. */
  reload: () => Promise<void>;
}

function currentOwner() {
  const user = useAuthStore.getState().authUser;
  return user?.can_use_personal_agent ? user.user_id : undefined;
}

/** Account-scoped navigation name; obsolete requests never publish into another account. */
export const useAgentNameStore = create<AgentNameState>((set, get) => {
  let revision = 0;
  return {
    owner: undefined,
    name: DEFAULT_AGENT_NAME,
    loaded: false,
    setName: (name) => {
      revision++;
      set({ owner: currentOwner(), name: name?.trim() || DEFAULT_AGENT_NAME, loaded: true });
    },
    load: async () => {
      const owner = currentOwner();
      if (!owner) {
        revision++;
        set({ owner: undefined, name: DEFAULT_AGENT_NAME, loaded: false });
        return;
      }
      if (get().owner === owner && get().loaded) return;
      const requestRevision = ++revision;
      set({ owner, name: get().owner === owner ? get().name : DEFAULT_AGENT_NAME, loaded: true });
      try {
        // Reading does not create an agent before its first visit.
        const agent = await personalRequest<PersonalAgent | null>('/v1/personal-agent');
        if (requestRevision === revision && currentOwner() === owner) {
          set({ name: agent?.config.name?.trim() || DEFAULT_AGENT_NAME });
        }
      } catch {
        if (requestRevision === revision && currentOwner() === owner) set({ loaded: false });
      }
    },
    reload: async () => {
      set({ loaded: false });
      await get().load();
    },
  };
});
