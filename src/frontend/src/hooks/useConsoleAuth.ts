import { useEffect, useState } from 'react';

interface UseConsoleAuthOpts {
  /** localStorage key holding the manually-entered token（ADMIN/CONFIG）。 */
  storageKey: string;
  /** Login-invalidation event name (dispatched by adminFetch/configFetch on 401/403). */
  expiredEvent: string;
  /** Probe a protected endpoint using the session cookie; resolve=authorized, reject=unauthorized. */
  probe: () => Promise<unknown>;
  /** Side effect on login invalidation (e.g. message.warning prompting the user to re-enter the token). */
  onExpired?: () => void;
}

/**
 * Unified auth state machine for the two consoles /config and /admin:
 * - Manually-entered token present → token mode;
 * - No token → probe a protected endpoint with the session cookie; if it passes, session mode (token-free direct access), otherwise fall back to entering a token;
 * - Logout: token mode clears the token and returns to the login page, session mode returns to the main app.
 */
export function useConsoleAuth({ storageKey, expiredEvent, probe, onExpired }: UseConsoleAuthOpts) {
  const [token, setToken] = useState<string | null>(localStorage.getItem(storageKey));
  const [sessionAuthed, setSessionAuthed] = useState(false);
  const [checking, setChecking] = useState(!localStorage.getItem(storageKey));

  useEffect(() => {
    if (localStorage.getItem(storageKey)) return;
    let alive = true;
    probe()
      .then(() => { if (alive) setSessionAuthed(true); })
      .catch(() => { /* unauthorized → show token login */ })
      .finally(() => { if (alive) setChecking(false); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);  // Probe only once on mount

  useEffect(() => {
    const handler = () => {
      localStorage.removeItem(storageKey);
      setToken(null);
      setSessionAuthed(false);
      onExpired?.();
    };
    window.addEventListener(expiredEvent, handler);
    return () => window.removeEventListener(expiredEvent, handler);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);  // Subscribe only once on mount

  const logout = () => {
    if (token) {
      localStorage.removeItem(storageKey);
      setToken(null);
    } else {
      // Session-mode logout → return to the main app
      window.location.href = '/';
    }
    setSessionAuthed(false);
  };

  // In session mode the token is an empty string, so child components' fetch('', …) automatically switch to the session cookie.
  return { token, setToken, sessionAuthed, checking, effToken: token ?? '', logout };
}
