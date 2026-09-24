import { create } from 'zustand';
import { apiRequest, getApiUrl, unwrapData, uploadFile } from '../api';
import { createTeamFolderBatch, uploadTeamFile } from '../editionApi';
import { useAuthStore } from './authStore';
import { assetUploadQueue, directoryPaths, type UploadSelection } from '../utils/folderUpload';
import { t } from '../i18n';

export interface UploadDestination { kind: 'personal' | 'team'; teamId?: string; folderId: string | null }
interface UploadItem {
  path: string; size: number; modified: number; key: string;
  status: 'pending' | 'uploading' | 'done' | 'failed'; error?: string;
}
export interface AssetUploadTask {
  id: string; owner: string; api: string; destination: UploadDestination;
  directories: string[]; items: UploadItem[];
  phase: 'reading' | 'directories' | 'uploading' | 'waiting' | 'finished' | 'failed' | 'reselect';
  error?: string; persisted: boolean;
}
interface State { task: AssetUploadTask | null; busy: boolean }
export const useAssetUploadStore = create<State>(() => ({ task: null, busy: false }));
const handles = new Map<string, File>();
const storageKey = (owner: string) => 'asset-upload-v1:' + getApiUrl() + ':' + owner;
function publish(task: AssetUploadTask) {
  const next = { ...task, items: task.items.map(item => ({ ...item })) };
  try { sessionStorage.setItem(storageKey(task.owner), JSON.stringify(next)); }
  catch { next.persisted = false; }
  useAssetUploadStore.setState({ task: next });
}
function guard(task: AssetUploadTask) {
  if (!task.owner || String(useAuthStore.getState().authUser?.user_id ?? '') !== task.owner || getApiUrl() !== task.api) {
    throw Object.assign(new Error(t('登录身份或服务地址已改变，请重新选择文件')), { status: 401 });
  }
}
export function restoreAssetUpload() {
  const owner = String(useAuthStore.getState().authUser?.user_id ?? '');
  const current = useAssetUploadStore.getState().task;
  if (current?.owner === owner && current.api === getApiUrl()) return;
  if (useAssetUploadStore.getState().busy) return;
  handles.clear();
  useAssetUploadStore.setState({ task: null });
  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey(owner)) || 'null') as AssetUploadTask | null;
    if (saved?.owner === owner && saved.api === getApiUrl() && Array.isArray(saved.items)) {
      saved.items.forEach(item => { if (item.status === 'uploading') item.status = 'pending'; });
      if (saved.items.some(item => item.status !== 'done') || saved.phase !== 'finished') saved.phase = 'reselect';
      publish(saved);
    }
  } catch { /* A corrupt or unavailable manifest never starts requests. */ }
}
export function clearAssetUpload() {
  if (useAssetUploadStore.getState().busy) return;
  const task = useAssetUploadStore.getState().task;
  if (task) { try { sessionStorage.removeItem(storageKey(task.owner)); } catch { /* optional persistence */ } }
  handles.clear();
  useAssetUploadStore.setState({ task: null });
}
export async function startAssetUpload(selectionPromise: Promise<UploadSelection>, destination: UploadDestination) {
  if (useAssetUploadStore.getState().busy) { void selectionPromise.catch(() => {}); return; }
  const previous = useAssetUploadStore.getState().task;
  const owner = String(useAuthStore.getState().authUser?.user_id ?? '');
  const api = getApiUrl();
  useAssetUploadStore.setState({ busy: true });
  try {
    const selection = await selectionPromise;
    const directories = directoryPaths(selection);
    if (!selection.files.length && !directories.length) return;
    let task: AssetUploadTask;
    if (previous && previous.owner === owner && previous.api === api && previous.phase !== 'finished') {
      const matches = JSON.stringify(directories) === JSON.stringify(previous.directories) && selection.files.length === previous.items.length && previous.items.every(item => selection.files.some(({file, path}) => item.path === path && item.size === file.size && item.modified === file.lastModified));
      if (!matches) throw new Error(t('请选择原上传文件，或先清除上传记录'));
      task = previous;
    } else {
      task = {
        id: crypto.randomUUID(), owner, api, destination: { ...destination }, directories,
        items: selection.files.map(({file, path}) => ({ path, size: file.size, modified: file.lastModified, key: crypto.randomUUID(), status: 'pending' })),
        phase: 'directories', persisted: true,
      };
      handles.clear();
    }
    if (new Set(task.items.map(item => item.path)).size !== task.items.length) throw new Error(t('上传清单包含重复路径'));
    for (const item of task.items) handles.set(item.key, selection.files.find(f => f.path === item.path)!.file);
    guard(task);
    publish(task);
    await execute(task);
  } finally { useAssetUploadStore.setState({ busy: false }); }
}
export async function retryAssetUpload() {
  const task = useAssetUploadStore.getState().task;
  if (!task || useAssetUploadStore.getState().busy) return;
  if (task.items.some(item => item.status !== 'done' && !handles.has(item.key))) {
    publish({ ...task, phase: 'reselect' }); return;
  }
  useAssetUploadStore.setState({ busy: true });
  try { await execute(task); } finally { useAssetUploadStore.setState({ busy: false }); }
}
async function execute(task: AssetUploadTask) {
  const { destination } = task;
  task.error = undefined;
  const waiting = () => { task.phase = 'waiting'; publish(task); };
  const request = <T,>(operation: () => Promise<T>) => assetUploadQueue.request(() => { guard(task); return operation(); }, waiting);
  const pathIds = new Map<string, string | null>([['', destination.folderId]]);
  try {
    task.phase = 'directories'; publish(task);
    for (let offset = 0; offset < task.directories.length; offset += 200) {
      const paths = task.directories.slice(offset, offset + 200);
      const response = await request(() => destination.kind === 'team'
        ? createTeamFolderBatch(destination.teamId!, destination.folderId, paths)
        : apiRequest<unknown>('/v1/myspace/folders/batch', {
          method: 'POST', body: JSON.stringify({ paths, parent_folder_id: destination.folderId }),
        }));
      const mapping = unwrapData<{ folders: Record<string, string> }>(response).folders;
      for (const path of paths) {
        if (!mapping?.[path]) throw new Error(t('服务器未返回完整目录，请重试'));
        pathIds.set(path, mapping[path]);
      }
    }
    const pending = task.items.filter(item => item.status !== 'done');
    let index = 0;
    const worker = async () => {
      while (index < pending.length) {
        const item = pending[index++];
        item.error = undefined;
        item.status = 'uploading'; task.phase = 'uploading'; publish(task);
        try {
          const file = handles.get(item.key);
          if (!file) throw new Error(t('请选择原上传文件，或先清除上传记录'));
          const parent = item.path.includes('/') ? item.path.slice(0, item.path.lastIndexOf('/')) : '';
          if (!pathIds.has(parent)) throw new Error(t('服务器未返回完整目录，请重试'));
          const folderId = pathIds.get(parent)!;
          await request<unknown>(() => destination.kind === 'team'
            ? uploadTeamFile(destination.teamId!, folderId, file, item.key)
            : uploadFile(file, undefined, folderId, { uploadKey: item.key }));
          item.status = 'done';
        } catch (error) {
          item.status = 'failed';
          item.error = error instanceof Error ? error.message : String(error);
        }
        publish(task);
      }
    };
    await Promise.all(Array.from({ length: Math.min(4, pending.length) }, worker));
    task.phase = task.items.some(item => item.status === 'failed') ? 'failed' : 'finished';
  } catch (error) {
    task.phase = 'failed'; task.error = error instanceof Error ? error.message : String(error);
  }
  publish(task);
}
