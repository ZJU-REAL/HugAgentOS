import { createStore } from 'zustand/vanilla';
import type { DeviceSkillFile } from '../api';

export interface DeviceSkillFileClient {
  read: (id: string) => Promise<DeviceSkillFile>;
  write: (id: string, content: string, expectedRevision: string) => Promise<DeviceSkillFile>;
}
export function createDeviceSkillEditorStore(installId: string, client: DeviceSkillFileClient, saved: () => Promise<void>) {
  let epoch = 0;
  return createStore<{
    file: DeviceSkillFile | null; content: string; loading: boolean; error: unknown;
    setContent: (content: string) => void; load: () => Promise<void>; save: () => Promise<void>; dispose: () => void;
  }>((set, get) => ({
    file: null, content: '', loading: false, error: null,
    setContent: (content) => set({ content }),
    load: async () => {
      const expected = ++epoch;
      set({ loading: true, error: null });
      try {
        const file = await client.read(installId);
        if (expected !== epoch) return;
        if (file.is_binary || !file.revision) throw new Error('SKILL.md is not an editable text revision');
        set({ file, content: file.content });
      } catch (error) { if (expected === epoch) set({ error }); }
      finally { if (expected === epoch) set({ loading: false }); }
    },
    save: async () => {
      const { file, content, loading } = get();
      if (!file || loading) return;
      const expected = ++epoch;
      set({ loading: true, error: null });
      try {
        const result = await client.write(installId, content, file.revision);
        if (expected !== epoch) return;
        set({ file: result, content: result.content });
        await saved();
      } catch (error) { if (expected === epoch) set({ error }); }
      finally { if (expected === epoch) set({ loading: false }); }
    },
    dispose: () => { epoch += 1; },
  }));
}
