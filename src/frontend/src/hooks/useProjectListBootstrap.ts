import { useEffect } from 'react';
import { useDeploymentModeStore } from '../stores/deploymentModeStore';
import { useProjectStore } from '../stores/projectStore';

/** The sidebar needs projects even when the project panel has never mounted. */
export function useProjectListBootstrap(userId?: string) {
  const localReady = useDeploymentModeStore(
    (s) => s.provisionMode === 'dual' && s.localReady,
  );

  useEffect(() => {
    const reset = useProjectStore.getState().resetProjectList;
    reset();
    // Invalidate pending responses before the next account or an unmount.
    return reset;
  }, [userId]);

  useEffect(() => {
    if (!userId) return;
    // Login can precede identity bridging. Repeat once the local proxy can serve
    // projects; unchanged SSE frames do not retrigger this effect.
    void useProjectStore.getState().fetchProjects();
  }, [userId, localReady]);
}
