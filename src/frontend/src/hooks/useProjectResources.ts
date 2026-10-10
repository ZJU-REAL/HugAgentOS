import { useEffect } from 'react';
import { useAuthStore } from '../stores/authStore';
import { useProjectStore } from '../stores/projectStore';

/** Project resources exist only while the project overview is open. */
export function useProjectResources(projectId: string | undefined) {
  const userId = useAuthStore(s => s.authUser?.user_id);
  useEffect(() => {
    const projects = useProjectStore.getState();
    if (!projectId || !userId) {
      projects.closeCurrentProject();
      return;
    }
    void projects.reloadProject(projectId);
    return () => projects.closeCurrentProject();
  }, [projectId, userId]);
}
