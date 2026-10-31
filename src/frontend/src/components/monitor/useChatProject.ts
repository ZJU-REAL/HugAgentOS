import { useCallback, useEffect, useState } from 'react';
import { getProject } from '../../api';
import { getProjectEnvironment, type ProjectEnvironment } from '../../api/projectEnvironment';
import type { ProjectDetail } from '../../types';

type EnvironmentMode = ProjectEnvironment['mode'];

export interface ChatProject {
  project: ProjectDetail;
  /** null when the environment is not readable for this user. */
  environment: EnvironmentMode | null;
  reloadEnvironment: () => void;
}

/** The project a chat belongs to and its environment mode, for the run monitor. */
export function useChatProject(projectId: string | undefined): ChatProject | null {
  // Keyed by project so switching chats never shows the previous project.
  const [state, setState] = useState<{ id: string; project: ProjectDetail | null; environment: EnvironmentMode | null } | null>(null);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    if (!projectId) return;
    let active = true;
    void Promise.all([
      getProject(projectId).catch(() => null),
      getProjectEnvironment(projectId).then((detail) => detail.config.mode).catch(() => null),
    ]).then(([project, environment]) => {
      if (active) setState({ id: projectId, project, environment });
    });
    return () => { active = false; };
  }, [projectId, revision]);

  const reloadEnvironment = useCallback(() => setRevision((n) => n + 1), []);
  if (!projectId || state?.id !== projectId || !state.project) return null;
  return { project: state.project, environment: state.environment, reloadEnvironment };
}
