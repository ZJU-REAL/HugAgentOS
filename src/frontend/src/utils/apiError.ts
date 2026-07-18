/**
 * Single source of truth for reading API errors—— shared by api.ts and adminApi.ts,
 * avoiding the "nested detail extraction + 402 guidance text" being copied in two places and drifting apart.
 */
import { t } from '../i18n';

type JsonObject = Record<string, unknown>;

/** License feature flag not authorized (HTTP 402, see backend core/licensing).
 * Identified by type rather than a message substring—— rewording/internationalizing the message will not break the 402 detection. */
export class LicenseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'LicenseError';
  }
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
    if (detail && typeof detail === 'object') {
      const nested = (detail as JsonObject).message;
      if (typeof nested === 'string' && nested.trim()) {
        return nested;
      }
    }
  }
  return fallback;
}


/** 402 = license feature flag not authorized (deliberately avoiding the logout semantics of 401/403). */
export function licenseErrorMessage(payload: unknown): string {
  const msg = readErrorMessage(payload, t('该功能未在当前 license 中授权'));
  return `${msg}${t('（请联系管理员在 系统配置 → License 中激活或更新 license）')}`;
}
