import { apiRequest, createProject, isLocalProject, unwrapData } from '../api';

export type EnvironmentField = 'agent_ids' | 'plugin_ids' | 'skill_ids' | 'mcp_ids';
export interface ProjectEnvironment {
  mode: 'all' | 'custom';
  agent_ids: string[];
  plugin_ids: string[];
  skill_ids: string[];
  mcp_ids: string[];
}
export interface EnvironmentResource {
  id: string;
  name: string;
  description?: string;
  dependencies: Partial<Record<EnvironmentField, string[]>>;
}
export type EnvironmentOptions = Record<EnvironmentField, EnvironmentResource[]>;
export interface EnvironmentPreview {
  effective: Record<EnvironmentField, string[]>;
  dependencies: Array<{ kind: EnvironmentField; id: string; name: string; required_by: string }>;
  unavailable: Array<{ kind: EnvironmentField; id: string; required_by?: string }>;
}
export interface EnvironmentDetail extends EnvironmentPreview {
  config: ProjectEnvironment;
  revision: string;
}
export const defaultProjectEnvironment = (): ProjectEnvironment => ({
  mode: 'all', agent_ids: [], plugin_ids: [], skill_ids: [], mcp_ids: [],
});

async function request<T>(path: string, options?: RequestInit, projectId?: string): Promise<T> {
  return unwrapData<T>(await apiRequest<unknown>(
    path, options, isLocalProject(projectId) ? 'local' : undefined,
  ));
}
export const getEnvironmentOptions = (projectId?: string) =>
  request<EnvironmentOptions>('/v1/project-environments/options', undefined, projectId);
export const previewEnvironment = (config: ProjectEnvironment, projectId?: string) =>
  request<EnvironmentPreview>('/v1/project-environments/preview', {
    method: 'POST', body: JSON.stringify(config),
  }, projectId);
export const getProjectEnvironment = (projectId: string) =>
  request<EnvironmentDetail>(`/v1/projects/${projectId}/environment`, undefined, projectId);
export const saveProjectEnvironment = (projectId: string, config: ProjectEnvironment) =>
  request<EnvironmentDetail>(`/v1/projects/${projectId}/environment`, {
    method: 'PUT', body: JSON.stringify(config),
  }, projectId);
export const createEnvironmentProject = (
  body: Parameters<typeof createProject>[0] & { environment?: ProjectEnvironment },
) => createProject(body);
