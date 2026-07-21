import { create } from 'zustand';

export interface CanvasArtifact {
  file_id: string;
  name: string;
  url: string;          // relative path, e.g. /files/xxx
  mime_type?: string;
  size?: number;
  chat_id?: string;     // for "save as" to associate with a conversation
}

export interface OntologyPanelTarget {
  chatId: string;
  messageTs: number;
}

export type RightSidebarView = 'file' | 'ontology' | 'empty';

interface CanvasState {
  isOpen: boolean;
  activeView: RightSidebarView;
  artifact: CanvasArtifact | null;
  ontologyTarget: OntologyPanelTarget | null;
  /** Incremented only by openCanvas — used to detect "new file opened" vs "same file saved" */
  openSeq: number;
  openCanvas: (artifact: CanvasArtifact) => void;
  openOntology: (target: OntologyPanelTarget) => void;
  openSidebar: () => void;
  closeCanvas: () => void;
  resetSidebar: () => void;
  /** Update artifact metadata without re-triggering content reload */
  updateArtifact: (patch: Partial<CanvasArtifact>) => void;
}

export const useCanvasStore = create<CanvasState>((set) => ({
  isOpen: false,
  activeView: 'empty',
  artifact: null,
  ontologyTarget: null,
  openSeq: 0,
  openCanvas: (artifact) => set((s) => ({
    isOpen: true,
    activeView: 'file',
    artifact,
    openSeq: s.openSeq + 1,
  })),
  openOntology: (ontologyTarget) => set({
    isOpen: true,
    activeView: 'ontology',
    ontologyTarget,
  }),
  openSidebar: () => set({ isOpen: true }),
  // Keep the selected content while collapsed so the top-right toggle can
  // restore the same view. Chat/panel changes call resetSidebar explicitly.
  closeCanvas: () => set({ isOpen: false }),
  resetSidebar: () => set({
    isOpen: false,
    activeView: 'empty',
    artifact: null,
    ontologyTarget: null,
  }),
  updateArtifact: (patch) => set((state) => ({
    artifact: state.artifact ? { ...state.artifact, ...patch } : null,
  })),
}));
