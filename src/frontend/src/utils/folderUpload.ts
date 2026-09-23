import { t } from '../i18n';

export interface UploadSelection {
  files: { file: File; path: string }[];
  directories: string[];
}
export function pickedFiles(files: FileList | File[]): UploadSelection {
  return { files: Array.from(files).map(file => ({ file, path: file.webkitRelativePath || file.name })), directories: [] };
}
export function directoryPaths(selection: UploadSelection): string[] {
  const dirs = new Set(selection.directories);
  for (const { path } of selection.files) {
    const parts = path.split('/');
    for (let n = 1; n < parts.length; n++) dirs.add(parts.slice(0, n).join('/'));
  }
  for (const path of [...dirs]) {
    const parts = path.split('/');
    if (parts.length > 8 || parts.some(p => !p || p !== p.trim() || p === '.' || p === '..' || p.includes('\\') || p.includes('\0') || p.length > 255)) {
      throw new Error(t('文件夹路径非法或超过 8 级'));
    }
    for (let n = 1; n < parts.length; n++) dirs.add(parts.slice(0, n).join('/'));
  }
  return [...dirs].sort((a, b) => a.split('/').length - b.split('/').length || a.localeCompare(b));
}

/** Capture entries synchronously while the browser's drag data store is readable. */
export function readDroppedFiles(data: DataTransfer): Promise<UploadSelection> {
  const items = Array.from(data.items ?? []).filter(item => item.kind === 'file');
  const roots = items.map(item => ({ entry: item.webkitGetAsEntry?.(), file: item.getAsFile() }));
  const fallback = Array.from(data.files);
  const result: UploadSelection = { files: [], directories: [] };
  const visit = async (entry: FileSystemEntry, prefix: string): Promise<void> => {
    const path = prefix + entry.name;
    if (entry.isFile) {
      const file = await new Promise<File>((resolve, reject) => (entry as FileSystemFileEntry).file(resolve, reject));
      result.files.push({ file, path });
    } else if (entry.isDirectory) {
      result.directories.push(path);
      const reader = (entry as FileSystemDirectoryEntry).createReader();
      for (;;) {
        const batch = await new Promise<FileSystemEntry[]>((resolve, reject) => reader.readEntries(resolve, reject));
        if (!batch.length) break;
        for (const child of batch) await visit(child, path + '/');
      }
    } else {
      throw new Error(t('无法读取拖入的文件夹'));
    }
  };
  return (async () => {
    if (!roots.length) return pickedFiles(fallback);
    for (const root of roots) {
      if (root.entry) await visit(root.entry, '');
      else if (root.file) result.files.push({ file: root.file, path: root.file.name });
      else throw new Error(t('无法读取拖入的文件夹'));
    }
    return result;
  })();
}

export interface UploadFailure extends Error { status?: number; retryAfterMs?: number }
const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

/** Shared request-start budget; retries consume the same slots as first attempts. */
export class UploadQueue {
  private active = 0;
  private nextStart = 0;
  private cooldown = 0;
  private pending: (() => void)[] = [];
  private timer: ReturnType<typeof setTimeout> | undefined;
  private interval: number;
  private concurrency: number;
  constructor(interval = 200, concurrency = 4) { this.interval = interval; this.concurrency = concurrency; }
  private pump() {
    if (this.timer || this.active >= this.concurrency || !this.pending.length) return;
    const wait = Math.max(this.nextStart, this.cooldown) - Date.now();
    if (wait > 0) {
      this.timer = setTimeout(() => { this.timer = undefined; this.pump(); }, wait);
      return;
    }
    this.active++;
    this.nextStart = Date.now() + this.interval;
    this.pending.shift()!();
    this.pump();
  }
  private once<T>(operation: () => Promise<T>): Promise<T> {
    return new Promise((resolve, reject) => {
      this.pending.push(() => {
        Promise.resolve().then(operation).then(resolve, reject).finally(() => { this.active--; this.pump(); });
      });
      this.pump();
    });
  }
  async request<T>(operation: () => Promise<T>, waiting?: () => void): Promise<T> {
    for (let attempt = 0; ; attempt++) {
      let delay = 0;
      try {
        return await this.once(async () => {
          try { return await operation(); }
          catch (error) {
            const e = error as UploadFailure;
            delay = e.retryAfterMs ?? (1000 * 2 ** attempt + Math.random() * 300);
            if (e.status === 429) {
              this.cooldown = Math.max(this.cooldown, Date.now() + delay);
              waiting?.();
            }
            throw error;
          }
        });
      } catch (error) {
        const e = error as UploadFailure;
        const transient = e instanceof TypeError || e.status === 408 || e.status === 429 || (e.status !== undefined && e.status >= 500);
        if (!transient || attempt >= 3) throw error;
        if (e.status !== 429) await sleep(delay);
      }
    }
  }
}
export const assetUploadQueue = new UploadQueue();
