import { apiRequest } from '../../api';

export interface FieldDefinition {
  name: string; type: string; required: boolean; unique: boolean; indexed: boolean; max_length: number;
}
export interface TableDefinition { name: string; columns: FieldDefinition[]; public_insert: boolean; public_read: boolean; public_replace: boolean }
export interface MCPToolDefinition {
  name: string; description: string; table: string; fields: string[]; filters: string[];
}
export interface MCPPublishReceipt {
  project_synced?: boolean; project_id?: string; url: string; token?: string; version: number; installed?: boolean; connection_verified?: boolean;
}
export interface Application {
  id: string; title: string; site_id: string | null; tables: Record<string, TableDefinition>;
  tools: MCPToolDefinition[];
  mcp_enabled: boolean; mcp_version?: number; project_id?: string | null;
}
export type Target = 'local' | 'cloud';
export type RecordRow = Record<string, unknown>;

export async function applicationRequest<T>(path: string, options?: RequestInit, target?: Target): Promise<T> {
  const response = await apiRequest<{ data: T }>(path, options, target);
  return response.data;
}


export interface ApplicationEditor {
  app_id: string; project_id: string; project_name: string; source_path: string; chat_id: string | null;
}
export function isMcpApplication(app: Application): boolean {
  return !!app.project_id || app.mcp_enabled || app.tools.length > 0;
}
