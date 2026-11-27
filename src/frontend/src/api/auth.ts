import type { EditionAuthUserFields } from '../editionApiTypes';
import { readErrorMessage } from '../utils/apiError';
import { t } from '../i18n';
import { apiRequest, getApiUrl, throwIfSessionExpired, unwrapData, type UserPreferences } from './core';

export async function getUserPreferences(userId: string): Promise<UserPreferences> {
  const wrapped = await apiRequest<unknown>(`/v1/users/${userId}/preferences`);
  return unwrapData<UserPreferences>(wrapped);
}

export async function updateUserPreferences(userId: string, preferences: UserPreferences): Promise<void> {
  await apiRequest(`/v1/users/${userId}/preferences`, {
    method: 'PUT',
    body: JSON.stringify(preferences),
  });
}

// ── Auth API (SSO session) ──────────────────────────────────────────────

export interface AuthUser extends EditionAuthUserFields {
  user_id: string;
  username: string;
  email?: string;
  avatar_url?: string;
  nickname?: string | null;
  real_name?: string | null;
  department?: string | null;
  expires_at?: string;
  sso_token?: string | null;
  /** null/undefined = all enabled apps visible by default; array = only the app IDs in the list are visible */
  allowed_apps?: string[] | null;
  /** undefined/true = Lab enabled by default; false = hide and forbid access to the Lab module */
  lab_enabled?: boolean;
  /** undefined/true = Sites enabled by default; false = hide the Sites module and refuse site management and publishing */
  sites_enabled?: boolean;
  /** Whether API keys may be used (default false; controlled by the Config admin platform) */
  can_use_api_key?: boolean;
  /** Whether the user may self-service add skills in the capability center (default false) */
  can_add_skill?: boolean;
  /** Whether the user may install skills from the skill marketplace (default false) */
  can_install_market_skill?: boolean;
  /** Whether the user may self-service add MCP tools in the capability center (default false) */
  can_add_mcp?: boolean;
  /** Whether the user may install MCPs from the MCP marketplace (default false) */
  can_install_market_mcp?: boolean;
  /** Whether the user may import plugin packages (zip / Claude Code / Codex) in the capability center (default false) */
  can_import_plugin?: boolean;
  /** Whether the user may install plugins from the plugin marketplace (default false) */
  can_install_market_plugin?: boolean;
  /** Whether the user may build or import their own sub-agents and submit them to the sub-agent marketplace (default false) */
  can_add_agent?: boolean;
  /** Whether the user may install sub-agents from the sub-agent marketplace (default false) */
  can_install_market_agent?: boolean;
  /** Whether the user may run the autonomous loop (the "autonomous loop" toggle in chat mode, default true) */
  can_run_autonomous_loop?: boolean;
  /** Whether the user may create their own private chat modes (设置 → 模式选择, default false) */
  can_manage_chat_modes?: boolean;
  /** Whether the user may create private knowledge bases (default false; visible only to the owner) */
  can_create_private_kb?: boolean;
  /** Whether the user may create public knowledge bases (default false; visible to everyone by default, can be further restricted by grants) */
  can_create_public_kb?: boolean;
  /** Whether the user may self-service create channel bots (inbound bots such as Feishu, default false) */
  can_create_channel_bot?: boolean;
  /** Whether the user may switch the currently available model from the chat input box (default false) */
  can_switch_model?: boolean;
  can_use_voice?: boolean;
  /** 会话评价: evaluation card, the 温馨提醒 reminder and history marks (default false) */
  can_use_evaluation?: boolean;
  /** Whether the user may configure and use ontology validation (default false) */
  can_use_ontology_validation?: boolean;
  /** Whether Personal Agent is available: module enabled on the server and permission granted (default false) */
  can_use_personal_agent?: boolean;
  /** Whether the user may enter the /config system settings console without a token (default false) */
  can_system_config?: boolean;
  /** Whether the user may enter the /admin content management console without a token (default false) */
  can_content_manage?: boolean;
  /** CE bootstrap accounts must replace the temporary default password before normal use. */
  must_change_password?: boolean;
  /** CE bootstrap owner must finish the browser first-run setup before entering the app shell. */
  onboarding_required?: boolean;
}

interface MyProfile extends AuthUser {
  user_center_id?: string;
  phone?: string | null;
  auth_source?: 'local' | 'external';
}

export async function getMyProfile(): Promise<MyProfile> {
  const wrapped = await apiRequest<unknown>('/v1/me');
  return unwrapData<MyProfile>(wrapped);
}

interface UpdateMyProfilePayload {
  nickname?: string;
  real_name?: string;
  phone?: string;
}

export async function updateMyProfile(payload: UpdateMyProfilePayload): Promise<MyProfile> {
  const wrapped = await apiRequest<unknown>('/v1/me', {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
  return unwrapData<MyProfile>(wrapped);
}

export async function changeMyPassword(
  oldPassword: string,
  newPassword: string,
): Promise<{ user_id: string; must_change_password: boolean }> {
  const wrapped = await apiRequest<unknown>('/v1/me/password', {
    method: 'PUT',
    body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
  });
  return unwrapData<{ user_id: string; must_change_password: boolean }>(wrapped);
}

export async function completeFirstRunSetup(): Promise<{
  user_id: string;
  onboarding_required: boolean;
  onboarding_completed_version: number;
}> {
  const wrapped = await apiRequest<unknown>('/v1/me/onboarding/complete', { method: 'POST' });
  return unwrapData<{
    user_id: string;
    onboarding_required: boolean;
    onboarding_completed_version: number;
  }>(wrapped);
}

interface AvatarUpdateResult {
  user_id: string;
  avatar_url: string | null;
}

/** Upload a custom avatar (multipart). Returns the new avatar_url (with a timestamp cache-busting parameter). */
export async function uploadMyAvatar(blob: Blob, filename = 'avatar.png'): Promise<AvatarUpdateResult> {
  const url = `${getApiUrl()}/v1/me/avatar`;
  const formData = new FormData();
  formData.append('file', blob, filename);

  const response = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    body: formData,
  });

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throwIfSessionExpired(response.status, payload);
    throw new Error(readErrorMessage(payload, t('头像上传失败: {status}', { status: response.status })));
  }
  return unwrapData<AvatarUpdateResult>(payload);
}

/** Set the avatar to a built-in default avatar URL (whitelist: /icons/avatar/avatar-{1-8}.png etc.). */
export async function setMyAvatarUrl(avatarUrl: string): Promise<AvatarUpdateResult> {
  const wrapped = await apiRequest<unknown>('/v1/me/avatar', {
    method: 'PUT',
    body: JSON.stringify({ avatar_url: avatarUrl }),
  });
  return unwrapData<AvatarUpdateResult>(wrapped);
}

/** Exchange a one-time SSO credential for a session cookie + user info.
 * Real OAuth2 SSO delivers the credential as `?code=`; local mock-SSO delivers
 * it as `?ticket=` — both are submitted to the backend in the `code` field. */
export async function exchangeSsoCredential(
  body: { code?: string },
): Promise<AuthUser> {
  const wrapped = await apiRequest<unknown>('/v1/auth/ticket/exchange', {
    method: 'POST',
    body: JSON.stringify(body),
  });
  return unwrapData<AuthUser>(wrapped);
}

/** Desktop plan B: exchange the current cookie session for a one-time handoff ticket.
 * Called only on the system-browser side after a successful login — once the ticket is
 * obtained the browser jumps to `hugagent://auth/callback?ticket=` to wake the desktop
 * app, which then uses the ticket against the backend directly to exchange it for the
 * real session token. */
export async function desktopHandoff(): Promise<string> {
  const wrapped = await apiRequest<unknown>('/v1/auth/desktop/handoff', {
    method: 'POST',
  });
  const data = unwrapData<{ handoff_ticket?: string }>(wrapped);
  if (!data?.handoff_ticket) {
    throw new Error('Missing handoff ticket');
  }
  return data.handoff_ticket;
}

/** Fetch the real OAuth authorize URL from the SSO login provider. Used by
 * the new provincial SSO flow where `/oa/login` returns
 * `{authorizeUrl: "..."}` and the browser must hop to that authorize URL. */
export async function getSsoAuthorizeUrl(): Promise<string | undefined> {
  const wrapped = await apiRequest<unknown>('/v1/auth/sso/authorize-url');
  const data = unwrapData<{ authorize_url?: string }>(wrapped);
  return data?.authorize_url || undefined;
}

/** Check if the current cookie session is still valid. */
export async function checkSession(): Promise<AuthUser> {
  const wrapped = await apiRequest<unknown>('/v1/auth/session/check');
  return unwrapData<AuthUser>(wrapped);
}

/** Revoke current session and clear the cookie.
 *
 * If `SSO_LOGOUT_URL` is configured on the backend, it returns an external
 * SSO logout URL via `logout_url`; the caller should redirect there so the
 * provider tears down its own session and bounces back via `redirect_uri`.
 * Otherwise the standard `login_url` fallback applies.
 */
export async function logout(): Promise<string | undefined> {
  const res = await apiRequest<unknown>('/v1/auth/logout', { method: 'POST' });
  const data = unwrapData<{ login_url?: string; logout_url?: string }>(res);
  return data?.logout_url || data?.login_url || undefined;
}
