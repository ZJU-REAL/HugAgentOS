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
  uploadingFiles: new Set(),
  importedSpaceFiles: [],

  setUploadedFiles: (files) => set({ uploadedFiles: files }),
  addUploadedFile: (file) => set((s) => ({ uploadedFiles: [...s.uploadedFiles, file] })),
  removeUploadedFile: (file) => set((s) => ({
    uploadedFiles: s.uploadedFiles.filter((f) => f !== file),
  })),
  clearUploadedFiles: () => set({ uploadedFiles: [] }),
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
