import { message } from 'antd';
import { t } from '../../i18n';
import { cancelMcpMarketOAuth, getMcpMarketOAuthStatus } from '../../api';
import { PENDING_USER_OAUTH_STATUSES } from '../../utils/constants';

/** One in-flight browser consent; `cancelled` is flipped by the dialog's cancel button. */
export interface McpOAuthAttempt {
  flowId: string;
  popup: Window | null;
  cancelled: boolean;
}

export const OAUTH_CANCELLED = '__mcp_oauth_cancelled__';

/**
 * Drive a started MCP OAuth flow through the popup the caller opened synchronously
 * (browsers only allow window.open inside the click), until the backend reports
 * completed or failed. Returns the flow result; throws OAUTH_CANCELLED on cancel.
 */
export async function runMcpOAuthPopup(
  attempt: McpOAuthAttempt,
  started: { flow_id: string; authorization_url: string },
  onStage: (stage: string) => void,
): Promise<{ server_id?: string; action?: string }> {
  const { popup } = attempt;
  if (!popup) throw new Error(t('浏览器阻止了 OAuth 登录窗口，请允许弹窗后重试'));
  attempt.flowId = started.flow_id;
  if (attempt.cancelled) {
    await cancelMcpMarketOAuth(started.flow_id);
    throw new Error(OAUTH_CANCELLED);
  }
  popup.opener = null;
  let authorizationUrl = started.authorization_url || '';
  if (authorizationUrl) {
    popup.location.href = authorizationUrl;
  } else {
    message.info(t('正在与服务方建立安全连接，请稍候…'));
  }
  let status = await getMcpMarketOAuthStatus(started.flow_id);
  for (let tick = 0; tick < 300 && !['completed', 'failed'].includes(status.status); tick += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
    if (attempt.cancelled) throw new Error(OAUTH_CANCELLED);
    status = await getMcpMarketOAuthStatus(started.flow_id);
    onStage(status.status);
    if (!authorizationUrl && status.authorization_url) {
      authorizationUrl = status.authorization_url;
      popup.location.href = authorizationUrl;
    }
    if (popup.closed && PENDING_USER_OAUTH_STATUSES.includes(status.status)) {
      // The user closed the consent tab; give a callback in flight one more second.
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      status = await getMcpMarketOAuthStatus(started.flow_id);
      if (PENDING_USER_OAUTH_STATUSES.includes(status.status)) {
        attempt.cancelled = true;
        await cancelMcpMarketOAuth(started.flow_id);
        throw new Error(OAUTH_CANCELLED);
      }
    }
  }
  popup.close();
  if (status.status !== 'completed') throw new Error(status.error || t('OAuth 登录超时或失败'));
  return status.result || {};
}
