import { create } from 'zustand';
import { message } from 'antd';
import { checkSession, desktopHandoff, exchangeSsoCredential, getSsoAuthorizeUrl, logout, onUnauthorized, type AuthUser } from '../api';
import { isEditionAccessError } from '../editionAccessError';
import { useChatStore } from './chatStore';
import { useAutomationChatStore } from './automationChatStore';

export const LOGIN_LANDING_KEY = 'hugagent_login_landing';
// Desktop client plan B: the system browser opens `<web>/?desktop=1` to start login;
// this flag survives the SSO round trip in sessionStorage (kept for the whole tab
// lifetime). After a successful login it is used to hand the session over to the
// desktop App via a one-time handoff ticket (deep-link wake-up) instead of rendering
// the app in the browser.
const DESKTOP_LOGIN_FLAG = 'hugagent_desktop_login';
// Custom protocol registered by the desktop App; the handoff ticket is passed back through it to wake the App.
const DESKTOP_CALLBACK_SCHEME = 'hugagent://auth/callback';

let authInitPromise: Promise<void> | null = null;
// Cached promise for the SSO authorize URL — stable per session, so we
// dedupe parallel/repeat lookups across 401 modals and logout.
let authorizeUrlPromise: Promise<string | undefined> | null = null;

interface AuthState {
  authUser: AuthUser | null;
  authChecking: boolean;
  authExpiredUrl: string | null;
  /** Whether user was ever authenticated in this session */
  wasAuthed: boolean;
  /** Logout in progress: used to render a full-screen overlay so the previous user's conversation content never flashes before the redirect */
  loggingOut: boolean;

  setAuthUser: (user: AuthUser | null) => void;
  setAvatarUrl: (avatarUrl: string | null) => void;
  setAuthChecking: (v: boolean) => void;
  setAuthExpiredUrl: (url: string | null) => void;
  triggerExpired: (loginUrl?: string) => void;
  initAuth: () => Promise<void>;
  doLogout: () => Promise<void>;
}

const SSO_LOGIN_URL = (import.meta.env.SSO_LOGIN_URL as string) || '';

function isSessionExpiredError(error: unknown): boolean {
  return error instanceof Error && error.message === 'Session expired';
}

function isMockLoginUrl(url?: string | null): boolean {
  const value = (url || '').trim();
  return !value || value.includes('/mock-sso/login');
}

/** Resolve the redirect-to-login URL.
 * Priority: 1) non-mock URL from 401 body  2) cached `/v1/auth/sso/authorize-url`
 * 3) `SSO_LOGIN_URL` env / mock-SSO landing.
 */
async function resolveLoginUrl(serverUrl?: string | null): Promise<string> {
  if (serverUrl && !isMockLoginUrl(serverUrl)) return serverUrl;

  if (!authorizeUrlPromise) {
    authorizeUrlPromise = getSsoAuthorizeUrl().catch(() => undefined);
  }
  const authorizeUrl = await authorizeUrlPromise;
  if (authorizeUrl && !isMockLoginUrl(authorizeUrl)) return authorizeUrl;
  // The provider lookup failed (or returned a mock URL); reset so a future
  // call can retry instead of being stuck with the cached failure.
  authorizeUrlPromise = null;

  if (SSO_LOGIN_URL && !isMockLoginUrl(SSO_LOGIN_URL)) return SSO_LOGIN_URL;
  const origin = window.location.origin;
  return `${origin}/mock-sso/login?redirect=${encodeURIComponent(window.location.pathname + window.location.search)}`;
}

/** Whether we are in the desktop login bridging flow (system-browser side). */
function isDesktopLogin(): boolean {
  try {
    return window.sessionStorage.getItem(DESKTOP_LOGIN_FLAG) === '1';
  } catch {
    return false;
  }
}

/** Whether we are running inside the desktop client (Tauri webview) — the shell injects `window.__TAURI__`. */
function inDesktopShell(): boolean {
  try {
    return typeof window !== 'undefined' && !!(window as unknown as {
      __TAURI__?: { core?: { invoke?: unknown } };
    }).__TAURI__?.core?.invoke;
  } catch {
    return false;
  }
}

/** Desktop logout: have the shell clear the local token and switch the window back to the native login page.
 * Returns true on success (the caller must NOT `window.location`-redirect to external SSO — that blanks the screen). */
async function logoutViaDesktopShell(): Promise<boolean> {
  if (!inDesktopShell()) return false;
  try {
    const core = (window as unknown as {
      __TAURI__: { core: { invoke: (cmd: string) => Promise<unknown> } };
    }).__TAURI__.core;
    await core.invoke('logout_desktop');
    return true;
  } catch {
    return false;
  }
}

/** Trigger `hugagent://` to wake the desktop App. Uses a hidden iframe to fire the custom
 * protocol, avoiding a top-level navigation that blanks the current page / leaves it
 * loading forever; browsers where the iframe has no effect fall back to top-level location. */
function triggerDesktopDeepLink(deeplink: string): void {
  try {
    const iframe = document.createElement('iframe');
    iframe.style.display = 'none';
    iframe.src = deeplink;
    document.body.appendChild(iframe);
    window.setTimeout(() => {
      try {
        document.body.removeChild(iframe);
      } catch {
        // ignore
      }
    }, 1500);
  } catch {
    try {
      window.location.href = deeplink;
    } catch {
      // ignore
    }
  }
}

/** Replace the "forever-spinning SPA" with a full-screen overlay giving a clear
 * "login succeeded, you can close this page" state, plus a "re-open the client" button.
 * The overlay uses native DOM so React does not keep rendering a spinner underneath. */
function showDesktopReturnOverlay(deeplink: string): void {
  if (document.getElementById('jx-desktop-return')) return;
  const o = document.createElement('div');
  o.id = 'jx-desktop-return';
  o.setAttribute(
    'style',
    'position:fixed;inset:0;z-index:2147483647;display:flex;align-items:center;justify-content:center;' +
      'background:radial-gradient(1200px 560px at 50% -12%,#E5EFFF 0%,rgba(229,239,255,0) 62%),linear-gradient(180deg,#FBFCFE 0%,#EEF2F8 100%);' +
      "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;color:#262626;",
  );
  o.innerHTML =
    '<div style="width:400px;max-width:90vw;padding:44px 40px 32px;text-align:center;background:#fff;' +
    'border:1px solid #E8EBF0;border-radius:22px;box-shadow:0 24px 70px rgba(18,109,255,.12),0 2px 10px rgba(15,23,42,.05)">' +
    '<div style="width:64px;height:64px;margin:0 auto 18px;border-radius:50%;background:#E9F8F2;display:flex;align-items:center;justify-content:center">' +
    '<svg width="34" height="34" viewBox="0 0 24 24" fill="none"><path d="M20 6L9 17l-5-5" stroke="#02B589" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
    '</div>' +
    '<h1 style="font-size:21px;font-weight:600;margin:0 0 8px">登录成功</h1>' +
    '<p style="font-size:13.5px;color:#808080;line-height:1.75;margin:0 0 28px">已唤起HugAgentOS桌面客户端，<br/>您可以关闭此页面了。</p>' +
    '<button id="jx-reopen" style="width:100%;height:46px;border:none;border-radius:12px;cursor:pointer;background:#126DFF;color:#fff;font-size:15px;font-weight:500">没有自动唤起？重新打开客户端</button>' +
    '<div id="jx-close" style="margin-top:14px;font-size:13px;color:#126DFF;cursor:pointer">关闭此页面</div>' +
    '</div>';
  document.body.appendChild(o);
  o.querySelector('#jx-reopen')?.addEventListener('click', () => triggerDesktopDeepLink(deeplink));
  o.querySelector('#jx-close')?.addEventListener('click', () => {
    try {
      window.close();
    } catch {
      // ignore
    }
  });
}

/** After a successful login, hand the session over to the desktop App: exchange a
 * one-time handoff ticket -> wake the App + show the "you can close this page" screen.
 * Returns true on success (the caller should stay in the bridging state and not render the app). */
async function bridgeToDesktop(): Promise<boolean> {
  if (!isDesktopLogin()) return false;
  try {
    const ticket = await desktopHandoff();
    try {
      window.sessionStorage.removeItem(DESKTOP_LOGIN_FLAG);
    } catch {
      // ignore
    }
    const deeplink = `${DESKTOP_CALLBACK_SCHEME}?ticket=${encodeURIComponent(ticket)}`;
    // First switch the page to the clear "login succeeded, closable" state, then wake the App (avoids this page spinning forever).
    showDesktopReturnOverlay(deeplink);
    triggerDesktopDeepLink(deeplink);
    // If the browser allows it (usually only for script-opened tabs), auto-close this
    // page after the wake-up; if it cannot be closed, keep the "closable" notice page
    // above instead of a loading state.
    window.setTimeout(() => {
      try {
        window.close();
      } catch {
        // ignore
      }
    }, 1500);
    return true;
  } catch {
    // Ticket fetch failed: stay in the browser and render the app as usual (the user
    // is at least logged in on the web side), without blocking; the desktop App side
    // will prompt a retry after not receiving the deep-link.
    return false;
  }
}

export const useAuthStore = create<AuthState>((set, get) => ({
  authUser: null,
  authChecking: true,
  authExpiredUrl: null,
  wasAuthed: false,
  loggingOut: false,

  setAuthUser: (user) => {
    if (user) set({ authUser: user, wasAuthed: true });
    else set({ authUser: user });
  },
  // Only updates the in-memory authUser (persistence already went through the backend /v1/me/avatar in the caller).
  setAvatarUrl: (avatarUrl) => {
    set((state) => ({
      authUser: state.authUser ? { ...state.authUser, avatar_url: avatarUrl || undefined } : state.authUser,
    }));
  },
  setAuthChecking: (v) => set({ authChecking: v }),
  setAuthExpiredUrl: (url) => set({ authExpiredUrl: url }),

  triggerExpired: (loginUrl?: string) => {
    const wasAuthed = get().wasAuthed;
    void resolveLoginUrl(loginUrl).then((url) => {
      if (wasAuthed) {
        set({ authExpiredUrl: url });
      } else {
        window.location.href = url;
      }
    });
  },

  initAuth: async () => {
    if (authInitPromise) {
      await authInitPromise;
      return;
    }

    authInitPromise = (async () => {
    // Register global 401 handler
      onUnauthorized((loginUrl: string) => {
        get().triggerExpired(loginUrl);
      });

      set({ authChecking: true });

      const params = new URLSearchParams(window.location.search);
      // Desktop: on first open with `?desktop=1`, record the intent (kept in
      // sessionStorage across the SSO round trip) and strip the param from the address
      // bar so later refreshes are not misinterpreted.
      if (params.get('desktop')) {
        try {
          window.sessionStorage.setItem(DESKTOP_LOGIN_FLAG, '1');
        } catch {
          // ignore (private mode etc.) — bridging degrades gracefully
        }
        params.delete('desktop');
        const cleaned = params.toString();
        window.history.replaceState(
          {},
          '',
          window.location.pathname + (cleaned ? `?${cleaned}` : ''),
        );
      }

      const code = params.get('code');
      const ticket = params.get('ticket');
      // Real OAuth2 uses ?code=, local mock-SSO uses ?ticket=; both are submitted to the backend as code.
      const credentialBody = code ? { code } : ticket ? { code: ticket } : null;

      if (credentialBody) {
        // Strip the one-time credential before exchanging — under React
        // StrictMode initAuth runs twice in dev, and a leftover value would
        // trigger a second exchange and incorrectly look like auth-expired.
        params.delete('code');
        params.delete('ticket');
        params.delete('redirect');
        const clean = params.toString();
        const newUrl = window.location.pathname + (clean ? `?${clean}` : '');
        window.history.replaceState({}, '', newUrl);

        try {
          const user = await exchangeSsoCredential(credentialBody);
          // Desktop: after a successful login, hand the session over to the App and stay in the bridging state (authChecking stays true, keeping the spinner).
          if (await bridgeToDesktop()) return;
          window.sessionStorage.setItem(LOGIN_LANDING_KEY, '1');
          set({ authUser: user, authChecking: false, wasAuthed: true });
          return;
        } catch (error) {
          if (isSessionExpiredError(error)) {
            set({ authUser: null, authChecking: false });
            return;
          }
          if (isEditionAccessError(error)) {
            // Edition access was rejected in remote mode: first try to restore
            // an existing session — an already-logged-in user re-entering with a stale
            // ?ticket= must not be falsely logged out; with no session, state the
            // reason and stay on the current page instead of starting another login loop.
            try {
              const user = await checkSession();
              set({ authUser: user, authChecking: false, wasAuthed: true });
            } catch {
              set({ authUser: null, authChecking: false });
              message.error(error.message, 10);
            }
            return;
          }
          // Fall through to session check
        }
      }

      try {
        const user = await checkSession();
        // Desktop: when the browser already has a valid session (user logged in before), exchange a ticket and hand over to the App directly.
        if (await bridgeToDesktop()) return;
        set({ authUser: user, authChecking: false, wasAuthed: true });
      } catch (error) {
        set({ authUser: null, authChecking: false });
        if (isEditionAccessError(error)) {
          // Edition access failures are not expired sessions and must not trigger a login loop.
          message.error(error.message, 10);
          return;
        }
        if (!isSessionExpiredError(error)) {
          get().triggerExpired();
        }
      }
    })();

    try {
      await authInitPromise;
    } finally {
      authInitPromise = null;
    }
  },

  doLogout: async () => {
    // Light up the full-screen overlay immediately (in parallel with await logout(),
    // not gating the hard redirect below) — it must appear before clearForLogout to
    // cover the intermediate frames caused by clearing the session.
    set({ loggingOut: true });
    let serverLoginUrl: string | undefined;
    try {
      serverLoginUrl = await logout();
    } catch {
      // ignore — cookie may already be gone
    }
    set({ authUser: null });
    // Detach chat state from the previous user so the redirect (or any UI
    // that renders during the redirect) can never paint their conversations.
    // Per-user localStorage entries stay on disk and are picked up again on
    // next login via hydrateForUser.
    useChatStore.getState().clearForLogout();
    useAutomationChatStore.getState().clearForLogout();
    // Reset cache so the next login picks a fresh authorize URL
    // (different OAuth2 nonce / state on each provider call).
    authorizeUrlPromise = null;
    // Desktop client: hand off to the shell to clear the token + switch back to the
    // native login page; never redirect to external SSO (in the WebView that gets
    // blocked by the navigation guard or lands on an empty route -> blank screen).
    if (await logoutViaDesktopShell()) return;
    const loginUrl = await resolveLoginUrl(serverLoginUrl);
    window.location.href = loginUrl;
  },
}));
