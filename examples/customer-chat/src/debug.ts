import './login.js';
import './developer.css';
const status = document.getElementById('session')!;
const result = document.getElementById('result')!;
async function session() {
 try {
  const response = await fetch('/api/v1/me', { credentials: 'same-origin' });
  if (response.status === 401) { status.textContent = 'Signed out'; return; }
  const body = await response.json();
  status.textContent = response.ok && body.code >= 10000 && body.code < 20000 && body.data?.user_id
   ? 'Signed in' : 'Backend error: ' + response.status;
 } catch { status.textContent = 'Backend unavailable. Configure HUGAGENT_BACKEND_URL and restart npm run dev.'; }
}
document.getElementById('request')!.addEventListener('click', () => {
 void (async () => {
  try {
   const path = (document.getElementById('endpoint') as HTMLSelectElement).value;
   const response = await fetch('/api' + path, { credentials: 'same-origin' });
   result.textContent = 'HTTP ' + response.status + '\n' + await response.text();
  } catch (error) { result.textContent = String(error); }
 })();
});
document.getElementById('logout')!.addEventListener('click', () => {
 void (async () => {
  try {
   const response = await fetch('/api/v1/auth/logout', { method: 'POST', credentials: 'same-origin' });
   if (!response.ok) throw new Error('Logout failed: ' + response.status);
   await session();
  } catch (error) { status.textContent = String(error); }
 })();
});
void session();
