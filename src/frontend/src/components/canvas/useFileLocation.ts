import { useCallback, useEffect, useState } from 'react';
import { authFetch, unwrapData } from '../../api';
import { artifactOrigin, artifactUrl, isArchiveMember, localArtifactLocationUrl } from '../../utils/artifactAccess';
import type { CanvasArtifact } from '../../stores/canvasStore';
import { listSpaceRoots } from '../myspace/spaceFilesApi';
import { t } from '../../i18n';

export function useFileLocation(file: CanvasArtifact, revision: number) {
  const [resolved, setResolved] = useState<{
    file: CanvasArtifact; revision: number; attempt: number; path: string;
    state: 'ready' | 'unfiled' | 'error';
  }>();
  const [attempt, setAttempt] = useState(0);
  const retry = useCallback(() => setAttempt(value => value + 1), []);
  const nativePath = useCallback((path: string) => {
    setResolved({ file, revision, attempt, path, state: 'ready' });
  }, [file, revision, attempt]);
  useEffect(() => {
    const controller = new AbortController();
    const load = async () => {
      if (isArchiveMember(file)) return { path: file.name };
      if (artifactOrigin(file) === 'cloud' && file.space) {
        const space = file.space;
        const root = space.name ? space : (await listSpaceRoots(controller.signal)).items.find(item => item.space_id === space.id);
        if (!root) throw new Error('Missing space');
        const base = root.kind === 'team' ? t('团队空间') : t('我的空间');
        return { path: [base, root.name, space.path].filter(Boolean).join('/') };
      }
      const local = artifactOrigin(file) === 'local';
      const url = local ? localArtifactLocationUrl(file)
        : artifactUrl({ ...file, url: '/files/' + encodeURIComponent(file.file_id) + '/location' });
      const response = await authFetch(url, { signal: controller.signal });
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const result = unwrapData<{ path?: string; kind?: string; root_name?: string; relative_path?: string }>(await response.json());
      if (local) {
        if (!result.path) throw new Error('Missing native path');
        return { path: result.path };
      }
      if (result.kind === 'unfiled') return { path: file.name, unfiled: true };
      if (!result.relative_path || !['personal', 'team'].includes(result.kind || '')) throw new Error('Invalid location');
      return { path: [result.kind === 'team' ? t('团队空间') : t('我的空间'), result.root_name, result.relative_path].filter(Boolean).join('/') };
    };
    void load().then(result => {
      if (controller.signal.aborted) return;
      setResolved({ file, revision, attempt, path: result.path, state: result.unfiled ? 'unfiled' : 'ready' });
    }).catch(() => {
      if (!controller.signal.aborted) { setResolved({ file, revision, attempt, path: file.name, state: 'error' }); }
    });
    return () => controller.abort();
  }, [file, revision, attempt]);
  const current = resolved?.file === file && resolved.revision === revision && resolved.attempt === attempt ? resolved : undefined;
  return { path: current?.path || '', state: current?.state || 'loading', retry, nativePath };
}
