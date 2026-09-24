/** Browser half of a device-bound login. The device secret never enters this page. */
import '../styles/variables.css';
import './desktopLogin.css';

const KEY = 'hugagent_desktop_request';
const PARAM = 'desktop_request';
const ROOT = '/api/v1/auth/desktop/requests/';
let mounted = false;

type Status = {
  status: 'pending' | 'approved' | 'delivered' | 'completed' | 'denied' | 'cancelled';
  confirm_code: string;
  username?: string;
  email?: string;
  account_id?: string;
  expires_in: number;
};

export function rememberDesktopRequest(): void {
  const id = new URLSearchParams(location.search).get(PARAM);
  if (id && /^[A-Za-z0-9_-]{43}$/.test(id)) {
    try { sessionStorage.setItem(KEY, JSON.stringify({ id, at: Date.now() })); } catch { /* URL remains usable */ }
  }
}

function requestId(): string | null {
  const id = new URLSearchParams(location.search).get(PARAM);
  if (id && /^[A-Za-z0-9_-]{43}$/.test(id)) return id;
  try {
    const value = JSON.parse(sessionStorage.getItem(KEY) || 'null');
    return value && Date.now() - value.at < 10 * 60_000 && /^[A-Za-z0-9_-]{43}$/.test(value.id)
      ? value.id : null;
  } catch { return null; }
}

export async function showDesktopApproval(): Promise<boolean> {
  const id = requestId();
  if (!id) return false;
  if (mounted) return true;
  mounted = true;
  // Keep request binding in this tab and in its refresh/SSO return URL.
  const url = new URL(location.href);
  url.searchParams.set(PARAM, id);
  history.replaceState({}, '', url);
  const referrer = document.createElement('meta');
  referrer.name = 'referrer';
  referrer.content = 'no-referrer';
  document.head.append(referrer);
  const overlay = document.createElement('main');
  overlay.id = 'desktop-approval';
  overlay.innerHTML = `
    <section class="desktop-approval-card" aria-labelledby="desktop-approval-title">
      <div class="desktop-approval-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4M9 10l2 2 4-4"/></svg>
      </div>
      <h1 id="desktop-approval-title">确认登录桌面端</h1>
      <div class="desktop-approval-account-box">
        <span class="desktop-approval-label">当前账号</span>
        <p id="desktop-approval-account"></p>
      </div>
      <div class="desktop-approval-code-box">
        <span class="desktop-approval-label">核对码</span>
        <strong id="desktop-approval-code"></strong>
      </div>
      <p id="desktop-approval-status" role="status">正在读取登录请求…</p>
      <div class="desktop-approval-actions">
        <button id="desktop-approve" disabled>确认登录桌面端</button>
        <button id="desktop-deny" disabled>拒绝</button>
        <button id="desktop-retry" hidden>重试</button>
        <a id="desktop-open" href="hugagent://auth/focus" hidden>打开桌面客户端</a>
      </div>
    </section>`;
  document.body.append(overlay);
  const el = <T extends HTMLElement>(selector: string) => overlay.querySelector<T>(selector)!;
  const status = el('#desktop-approval-status');
  const approve = el<HTMLButtonElement>('#desktop-approve');
  const deny = el<HTMLButtonElement>('#desktop-deny');
  const retry = el<HTMLButtonElement>('#desktop-retry');
  let state: Status | null = null;
  let stopped = false;
  let busy = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let deadline = 0;
  let failures = 0;

  async function api(action = ''): Promise<Status> {
    const response = await fetch(ROOT + encodeURIComponent(id!) + action, {
      method: action ? 'POST' : 'GET',
      credentials: 'same-origin', cache: 'no-store',
      headers: action ? { 'Content-Type': 'application/json' } : {},
      body: action ? JSON.stringify({ confirm_code: state?.confirm_code, account_id: state?.account_id }) : undefined,
      signal: AbortSignal.timeout(10_000),
    });
    if (!response.ok) {
      const message = response.status === 410 ? '登录请求已过期，请回桌面端重新登录。'
        : response.status === 401 ? '浏览器登录已过期，请刷新页面重新登录。'
        : response.status === 403 ? '当前账号或页面来源不匹配，请从桌面端重新登录。'
        : response.status === 409 ? '账号或请求状态已变化，请重试并重新核对账号。'
        : '暂时无法连接，请重试。';
      if ([401, 403, 410].includes(response.status)) stopped = true;
      throw new Error(message);
    }
    return (await response.json()).data as Status;
  }

  function render(next: Status) {
    state = { ...state, ...next };
    failures = 0;
    deadline = Date.now() + next.expires_in * 1000;
    if (next.username !== undefined) el('#desktop-approval-account').textContent =
      next.username + (next.email ? '（' + next.email + '）' : '');
    el('#desktop-approval-code').textContent = next.confirm_code;
    const messages: Record<Status['status'], string> = {
      pending: '请求将在 ' + Math.floor(next.expires_in / 60) + ':' + String(next.expires_in % 60).padStart(2, '0') + ' 后过期',
      approved: '已确认，正在等待桌面端接收登录结果。可以直接切回桌面端。',
      delivered: '桌面端正在完成登录，请稍候。',
      completed: '桌面端已登录成功，您可以关闭此页面。',
      denied: '已拒绝登录。需要登录时，请从桌面端重新发起。',
      cancelled: '登录已取消，请从桌面端重新发起。',
    };
    status.textContent = messages[next.status];
    approve.disabled = deny.disabled = next.status !== 'pending';
    approve.hidden = deny.hidden = next.status !== 'pending';
    stopped = ['completed', 'denied', 'cancelled'].includes(next.status);
    el('#desktop-open').hidden = next.status !== 'completed';
    retry.hidden = true;
    if (stopped) {
      try { sessionStorage.removeItem(KEY); } catch { /* no storage */ }
    }
  }

  function failure(error: unknown) {
    failures += 1;
    status.textContent = error instanceof Error ? error.message : '暂时无法连接，请重试。';
    approve.disabled = deny.disabled = true;
    retry.hidden = stopped;
  }

  function schedule() {
    clearTimeout(timer);
    if (!stopped) timer = setTimeout(() => void refresh(), Math.min(10_000, 2000 * 2 ** Math.min(failures, 3)));
  }

  async function refresh() {
    if (busy || stopped) return;
    if (deadline && Date.now() >= deadline) {
      stopped = true;
      failure(new Error('登录请求已过期，请回桌面端重新登录。'));
      return;
    }
    busy = true;
    approve.disabled = deny.disabled = true;
    try { render(await api()); } catch (error) { failure(error); }
    finally { busy = false; schedule(); }
  }

  async function decide(action: string) {
    if (busy || stopped || state?.status !== 'pending') return;
    busy = true;
    approve.disabled = deny.disabled = true;
    clearTimeout(timer);
    try { render(await api(action)); } catch (error) { failure(error); }
    finally { busy = false; schedule(); }
  }

  approve.onclick = () => void decide('/approve');
  deny.onclick = () => void decide('/deny');
  retry.onclick = () => void refresh();
  window.addEventListener('pagehide', () => { stopped = true; clearTimeout(timer); }, { once: true });
  await refresh();
  return true;
}
