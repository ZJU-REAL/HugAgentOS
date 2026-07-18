import { create } from 'zustand';

export interface CanvasArtifact {
  file_id: string;
  name: string;
  url: string;          // relative path, e.g. /files/xxx
  mime_type?: string;
  size?: number;
  chat_id?: string;     // for "save as" to associate with a conversation
}

interface CanvasState {
  isOpen: boolean;
  artifact: CanvasArtifact | null;
  /** Incremented only by openCanvas — used to detect "new file opened" vs "same file saved" */
  openSeq: number;
  openCanvas: (artifact: CanvasArtifact) => void;
  closeCanvas: () => void;
  /** Update artifact metadata without re-triggering content reload */
  updateArtifact: (patch: Partial<CanvasArtifact>) => void;
}

export const useCanvasStore = create<CanvasState>((set) => ({
  isOpen: false,
  artifact: null,
  openSeq: 0,
  openCanvas: (artifact) => set((s) => ({ isOpen: true, artifact, openSeq: s.openSeq + 1 })),
  closeCanvas: () => set({ isOpen: false, artifact: null }),
  updateArtifact: (patch) => set((state) => ({
    artifact: state.artifact ? { ...state.artifact, ...patch } : null,
  })),
}));
