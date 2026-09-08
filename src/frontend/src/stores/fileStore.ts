import { create } from 'zustand';

export interface ImportedSpaceFile {
  name: string;
  file_id: string;
  download_url: string;
  mime_type: string;
  type: 'document' | 'image';
}

interface FileState {
  uploadedFiles: File[];
  uploadedArtifacts: Map<File, { file_id: string; download_url: string }>;
  setUploadedArtifact: (file: File, artifact: { file_id: string; download_url: string }) => void;
  uploadingFiles: Set<File>;
  importedSpaceFiles: ImportedSpaceFile[];

  setUploadedFiles: (files: File[]) => void;
  addUploadedFile: (file: File) => void;
  removeUploadedFile: (file: File) => void;
  clearUploadedFiles: () => void;
  setUploadingFiles: (files: Set<File>) => void;
  addUploadingFile: (file: File) => void;
  removeUploadingFile: (file: File) => void;
  addImportedSpaceFiles: (files: ImportedSpaceFile[]) => void;
  removeImportedSpaceFile: (index: number) => void;
  clearImportedSpaceFiles: () => void;
}

export const useFileStore = create<FileState>((set) => ({
  uploadedFiles: [],
  uploadedArtifacts: new Map(),
  setUploadedArtifact: (file, artifact) => set((s) => {
    if (!s.uploadedFiles.includes(file)) return {};
    const uploadedArtifacts = new Map(s.uploadedArtifacts);
    uploadedArtifacts.set(file, artifact);
    return { uploadedArtifacts };
  }),
  uploadingFiles: new Set(),
  importedSpaceFiles: [],

  setUploadedFiles: (files) => set((s) => ({
    uploadedFiles: files,
    uploadedArtifacts: new Map([...s.uploadedArtifacts].filter(([file]) => files.includes(file))),
  })),
  addUploadedFile: (file) => set((s) => ({ uploadedFiles: [...s.uploadedFiles, file] })),
  removeUploadedFile: (file) => set((s) => {
    const uploadedArtifacts = new Map(s.uploadedArtifacts);
    uploadedArtifacts.delete(file);
    return { uploadedFiles: s.uploadedFiles.filter((f) => f !== file), uploadedArtifacts };
  }),
  clearUploadedFiles: () => set({ uploadedFiles: [], uploadedArtifacts: new Map() }),
  setUploadingFiles: (files) => set({ uploadingFiles: files }),
  addUploadingFile: (file) => set((s) => {
    const next = new Set(s.uploadingFiles);
    next.add(file);
    return { uploadingFiles: next };
  }),
  removeUploadingFile: (file) => set((s) => {
    const next = new Set(s.uploadingFiles);
    next.delete(file);
    return { uploadingFiles: next };
  }),
  addImportedSpaceFiles: (files) => set((s) => ({ importedSpaceFiles: [...s.importedSpaceFiles, ...files] })),
  removeImportedSpaceFile: (index) => set((s) => ({ importedSpaceFiles: s.importedSpaceFiles.filter((_, i) => i !== index) })),
  clearImportedSpaceFiles: () => set({ importedSpaceFiles: [] }),
}));
