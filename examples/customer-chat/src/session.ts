// Remove one-time credentials before any API call or further navigation.
const url = new URL(location.href);
const state = url.searchParams.get('state');
const started = sessionStorage.getItem('hugagent-login');
sessionStorage.removeItem('hugagent-login');
const code = url.searchParams.get('code') || url.searchParams.get('ticket');
history.replaceState(null, '', '/session.html');
void (async () => {
 try {
  const expected = started ? JSON.parse(started) : null;
  if (!state || expected?.state !== state || Date.now() - expected.started > 300000) throw new Error('Login state missing or expired. Start login from API debug.');
  if (!code) throw new Error('No login credential. Use the login link.');
  const response = await fetch('/api/v1/auth/ticket/exchange', { method: 'POST', credentials: 'same-origin',
   headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }) });
  const body = await response.json();
  if (!response.ok || body.code < 10000 || body.code >= 20000) throw new Error(body.message || 'Login exchange failed');
  location.replace('/debug.html');
 } catch (error) { document.getElementById('session')!.textContent = String(error); }
})();
export {};
