/**
 * Single source of truth for reading API errors—— shared by api.ts and adminApi.ts,
 * avoiding the "nested detail extraction + 402 guidance text" being copied in two places and drifting apart.
 */
import { t } from '../i18n';

type JsonObject = Record<string, unknown>;

export interface OntologyToolDetail {
  name: string;
  display_name: string;
}

export interface OntologyMcpBindingSuggestion {
  server_id: string;
  display_name: string;
  provided_tools: OntologyToolDetail[];
}

export interface OntologyBuildIssueDetails {
  missing_tools: Array<OntologyToolDetail & {
    mcp_servers: Array<{ server_id: string; display_name: string }>;
  }>;
  recommended_mcp_servers: OntologyMcpBindingSuggestion[];
  unmapped_tools: OntologyToolDetail[];
}

export interface OntologyBuildIssue {
  severity: string;
  code: string;
  message: string;
  workflow_id: string | null;
  details: OntologyBuildIssueDetails;
}

export interface OntologyBuildValidationReport {
  valid: false;
  matched_workflows: string[];
  resolved_tools: string[];
  resolved_tool_details: OntologyToolDetail[];
  errors: OntologyBuildIssue[];
  warnings: OntologyBuildIssue[];
  suggestions: string[];
}

export interface OntologyBuildFailure {
  message: string;
  report: OntologyBuildValidationReport;
}

/** License feature flag not authorized (HTTP 402, see backend core/licensing).
 * Identified by type rather than a message substring—— rewording/internationalizing the message will not break the 402 detection. */
export class LicenseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'LicenseError';
  }
}

/** HTTP 错误的结构化载体。保留后端完整响应，界面可展示校验报告等详细信息。 */
export class ApiResponseError extends Error {
  readonly status: number;
  readonly payload: unknown;
  readonly data: unknown;

  constructor(message: string, status: number, payload: unknown) {
    super(message);
    this.name = 'ApiResponseError';
    this.status = status;
    this.payload = payload;
    this.data = readErrorData(payload);
  }
}

function isRecord(value: unknown): value is JsonObject {
  return typeof value === 'object' && value !== null;
}

function readErrorData(payload: unknown): unknown {
  if (!isRecord(payload)) return undefined;
  if ('data' in payload) return payload.data;
  if (isRecord(payload.detail) && 'data' in payload.detail) return payload.detail.data;
  return undefined;
}

function readStringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : [];
}

function readToolDetails(value: unknown): OntologyToolDetail[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!isRecord(item) || typeof item.name !== 'string') return [];
    return [{
      name: item.name,
      display_name: typeof item.display_name === 'string' ? item.display_name : item.name,
    }];
  });
}

function readIssueDetails(value: unknown): OntologyBuildIssueDetails {
  if (!isRecord(value)) {
    return { missing_tools: [], recommended_mcp_servers: [], unmapped_tools: [] };
  }
  const missingTools = Array.isArray(value.missing_tools)
    ? value.missing_tools.flatMap((item) => {
        if (!isRecord(item) || typeof item.name !== 'string') return [];
        const servers = Array.isArray(item.mcp_servers)
          ? item.mcp_servers.flatMap((server) => (
              isRecord(server)
              && typeof server.server_id === 'string'
              && typeof server.display_name === 'string'
                ? [{ server_id: server.server_id, display_name: server.display_name }]
                : []
            ))
          : [];
        return [{
          name: item.name,
          display_name: typeof item.display_name === 'string' ? item.display_name : item.name,
          mcp_servers: servers,
        }];
      })
    : [];
  const recommended = Array.isArray(value.recommended_mcp_servers)
    ? value.recommended_mcp_servers.flatMap((item) => (
        isRecord(item)
        && typeof item.server_id === 'string'
        && typeof item.display_name === 'string'
          ? [{
              server_id: item.server_id,
              display_name: item.display_name,
              provided_tools: readToolDetails(item.provided_tools),
            }]
          : []
      ))
    : [];
  return {
    missing_tools: missingTools,
    recommended_mcp_servers: recommended,
    unmapped_tools: readToolDetails(value.unmapped_tools),
  };
}

function readFirstIssueMessage(value: unknown): string | null {
  if (!Array.isArray(value)) return null;
  for (const item of value) {
    if (!isRecord(item)) continue;
    const candidate = typeof item.msg === 'string' ? item.msg : item.message;
    if (typeof candidate === 'string' && candidate.trim()) return candidate;
  }
  return null;
}

function readIssues(value: unknown, fallbackSeverity: string): OntologyBuildIssue[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!isRecord(item) || typeof item.message !== 'string') return [];
    return [{
      severity: typeof item.severity === 'string' ? item.severity : fallbackSeverity,
      code: typeof item.code === 'string' ? item.code : 'validation_error',
      message: item.message,
      workflow_id: typeof item.workflow_id === 'string' ? item.workflow_id : null,
      details: readIssueDetails(item.details),
    }];
  });
}

/** 构造一个不会丢失响应 data 的请求异常。 */
export function createApiResponseError(
  status: number,
  payload: unknown,
  fallback: string,
): ApiResponseError {
  return new ApiResponseError(readErrorMessage(payload, fallback), status, payload);
}

/**
 * 从任意 catch 值中识别“本体构建校验失败”。
 * 解析逻辑刻意依赖报告结构，而不是中文报错文案，避免后端换文案或切换语言后失效。
 */
export function getOntologyBuildFailure(error: unknown): OntologyBuildFailure | null {
  if (!(error instanceof ApiResponseError) || !isRecord(error.data)) return null;
  const report = error.data;
  if (report.valid !== false || !Array.isArray(report.errors)) return null;

  return {
    message: error.message,
    report: {
      valid: false,
      matched_workflows: readStringArray(report.matched_workflows),
      resolved_tools: readStringArray(report.resolved_tools),
      resolved_tool_details: readToolDetails(report.resolved_tool_details),
      errors: readIssues(report.errors, 'error'),
      warnings: readIssues(report.warnings, 'warning'),
      suggestions: readStringArray(report.suggestions),
    },
  };
}

/** Extracts human-readable info from an error response body: top-level message → string detail →
 * structured detail.message (covers both FastAPI HTTPException / AppException envelopes). */
export function readErrorMessage(payload: unknown, fallback: string): string {
  if (payload && typeof payload === 'object') {
    const record = payload as JsonObject;
    const message = record.message;
    if (typeof message === 'string' && message.trim()) {
      return message;
    }
    const detail = record.detail;
    if (typeof detail === 'string' && detail.trim()) {
      return detail;
    }
    const detailIssue = readFirstIssueMessage(detail);
    if (detailIssue) return detailIssue;
    if (detail && typeof detail === 'object') {
      const nested = (detail as JsonObject).message;
      if (typeof nested === 'string' && nested.trim()) {
        return nested;
      }
    }
    const topLevelIssue = readFirstIssueMessage(record.errors);
    if (topLevelIssue) return topLevelIssue;
  }
  return fallback;
}


/** 402 = license feature flag not authorized (deliberately avoiding the logout semantics of 401/403). */
export function licenseErrorMessage(payload: unknown): string {
  const msg = readErrorMessage(payload, t('该功能未在当前 license 中授权'));
  return `${msg}${t('（请联系管理员在 系统配置 → License 中激活或更新 license）')}`;
}
