import type { ResourceItem } from '../../types';
import { useCanvasStore } from '../../stores/canvasStore';
import { useCanvasGuard } from '../canvas/useCanvasGuard';
import { listSpaceRoots } from './spaceFilesApi';
import { message } from 'antd';

export function useSpaceCanvas() {
  const guard = useCanvasGuard();
  const open = useCanvasStore(state => state.openCanvas);
  return (item: ResourceItem) => {
    guard(() => {
      void (async () => {
        const url = item.download_url || '/files/' + encodeURIComponent(item.file_id || '');
        const parsed = new URL(url, window.location.href);
        const id = parsed.searchParams.get('space');
        const path = parsed.searchParams.get('path');
        const root = id ? (await listSpaceRoots()).items.find(root => root.space_id === id) : undefined;
        if (id && !root) throw new Error('无权访问此文件');
        open({ file_id: item.file_id || item.id, name: item.name, url,
          size: item.size, mime_type: item.mime_type, origin: item.origin,
          ...(root && path !== null ? { space: { id: root.space_id, path, name: root.name, kind: root.kind, writable: root.permission !== 'view' } } : {}),
        });
      })().catch(error => message.error(error instanceof Error ? error.message : '无法打开文件'));
    });
  };
}
