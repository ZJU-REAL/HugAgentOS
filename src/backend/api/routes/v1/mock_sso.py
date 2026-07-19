"""Mock SSO server for local development and testing.

Provides two endpoints that simulate the external unified login system:
  - GET  /mock-sso/login          → Generates a one-time ticket and redirects to the app
  - POST /mock-sso/ticket/exchange → Validates the ticket and returns user info

Enable with:  SSO_MOCK_ENABLED=true  in .env

To test the full flow manually:
  1. Open http://localhost:3001/mock-sso/login?redirect=/ in your browser
  2. The mock SSO will generate a ticket and redirect to the app with ?ticket=...
"""

import os
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

from core.content.content_blocks import get_branding_info, is_register_allowed
from core.infra.logging import get_logger
from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

logger = get_logger(__name__)

router = APIRouter(prefix="/mock-sso", tags=["Mock SSO"])

# ── Mock-SSO ticket store ─────────────────────────────────────────────────
# Moved to core.auth.mock_ticket_store so core.auth.sso can validate tickets
# without importing this route module. Re-exported under original names.
from core.auth.mock_ticket_store import consume_ticket
from core.auth.mock_ticket_store import generate_ticket as _generate_ticket  # noqa: E402

# Predefined mock users with passwords
# The password field is only used for mock login verification; it is never sent to business systems with user_info
MOCK_USERS = [
    {
        "user_center_id": "sso_zhangsan_001",
        "username": "张三",
        "email": "zhangsan@example.com",
        "avatar_url": None,
        "password": "zhangsan123",
    },
    {
        "user_center_id": "sso_lisi_002",
        "username": "李四",
        "email": "lisi@example.com",
        "avatar_url": None,
        "password": "lisi@456",
    },
    {
        "user_center_id": "sso_wangwu_003",
        "username": "王五",
        "email": "wangwu@example.com",
        "avatar_url": None,
        "password": "wangwu#789",
    },
    {
        "user_center_id": "sso_gongxinyuan_004",
        "username": "gongxinyuan",
        "email": "gongxinyuan@example.com",
        "avatar_url": None,
        "password": "gongxinyuan123",
    },
]

# username → user dict, for quick lookup
_USER_BY_NAME: Dict[str, Dict[str, Any]] = {u["username"]: u for u in MOCK_USERS}


def _user_info_without_password(user: Dict[str, Any]) -> Dict[str, Any]:
    """Return user info without the password field, for downstream business use."""
    return {k: v for k, v in user.items() if k != "password"}


def _mock_account_shortcuts_enabled() -> bool:
    """Allow known mock users and password-free shortcuts only in explicit dev mode."""
    try:
        from core.config.settings import settings as _settings

        return _settings.edition.edition != "ce" and _settings.sso.effective_login_mode == "mock"
    except Exception:
        return False


def _resolve_frontend_origin(request: Request, redirect: str) -> str:
    """Resolve the frontend origin dynamically instead of hard-coding one port."""
    configured = os.getenv("MOCK_SSO_APP_BASE", "").strip()
    if configured:
        return configured.rstrip("/")

    parsed_redirect = urlparse(redirect or "")
    if parsed_redirect.scheme and parsed_redirect.netloc:
        return f"{parsed_redirect.scheme}://{parsed_redirect.netloc}"

    for header in ("origin", "referer"):
        raw = (request.headers.get(header) or "").strip()
        if not raw:
            continue
        parsed = urlparse(raw)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"

    forwarded_host = (request.headers.get("x-forwarded-host") or "").strip()
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").strip() or request.url.scheme
    if forwarded_host:
        return f"{forwarded_proto}://{forwarded_host}"

    # No-Docker local single-origin mode: frontend and backend share this
    # process/port, so the correct origin is simply the request's own host — never
    # the compose-era localhost:3000 nginx guess.
    from core.config.settings import settings as _settings

    if _settings.deploy.is_local:
        return f"{request.url.scheme}://{request.url.netloc}"

    frontend_port = os.getenv("FRONTEND_PORT", "3000")
    return f"http://localhost:{frontend_port}"


def _build_redirect_target(request: Request, redirect: str, ticket: str) -> str:
    """Redirect back to the frontend page that initiated the login flow."""
    frontend_origin = _resolve_frontend_origin(request, redirect)
    parsed_redirect = urlparse(redirect or "/")

    redirect_path = parsed_redirect.path or "/"
    redirect_query = parsed_redirect.query
    redirect_fragment = parsed_redirect.fragment

    existing_query = dict(parse_qsl(redirect_query, keep_blank_values=True))
    existing_query["ticket"] = ticket
    existing_query["redirect"] = redirect_path

    return urlunparse(
        (
            urlparse(frontend_origin).scheme,
            urlparse(frontend_origin).netloc,
            redirect_path,
            "",
            urlencode(existing_query, doseq=True),
            redirect_fragment,
        )
    )


def _render_register_form(redirect: str, error_html: str, action: str = "register") -> str:
    """Render the registration form section (reuses the login page CSS)."""
    return f"""
          <div class="form-panel" id="panel-register">
            <form method="POST" action="{action}" id="registerForm" novalidate>
              <input type="hidden" name="redirect" value="{redirect}"/>
              <div>
                <label class="sr-only" for="reg-code">注册码</label>
                <div class="field-shell">
                  <span class="field-icon" aria-hidden="true">
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                      <path d="M12 3l8 4v5c0 5-3.5 8.5-8 9c-4.5-.5-8-4-8-9V7l8-4z"></path>
                    </svg>
                  </span>
                  <input type="text" name="code" id="reg-code" placeholder="请输入注册码" data-ph="请输入注册码" autocomplete="off" required/>
                </div>
              </div>
              <div>
                <label class="sr-only" for="reg-username">账号</label>
                <div class="field-shell">
                  <span class="field-icon field-user" aria-hidden="true">
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                      <path d="M12 12a4.2 4.2 0 1 0 0-8.4a4.2 4.2 0 0 0 0 8.4Z"></path>
                      <path d="M4.8 19.2a7.2 7.2 0 0 1 14.4 0"></path>
                    </svg>
                  </span>
                  <input type="text" name="username" id="reg-username" placeholder="账号（仅英文、数字、下划线，2-32 位）" data-ph="账号（仅英文、数字、下划线，2-32 位）" autocomplete="username" pattern="[A-Za-z0-9_]{{2,32}}" required/>
                </div>
              </div>
              <div>
                <label class="sr-only" for="reg-nickname">用户名</label>
                <div class="field-shell">
                  <span class="field-icon field-user" aria-hidden="true">
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                      <path d="M12 12a4.2 4.2 0 1 0 0-8.4a4.2 4.2 0 0 0 0 8.4Z"></path>
                      <path d="M4.8 19.2a7.2 7.2 0 0 1 14.4 0"></path>
                    </svg>
                  </span>
                  <input type="text" name="nickname" id="reg-nickname" placeholder="请输入用户名（昵称，最多 32 位）" data-ph="请输入用户名（昵称，最多 32 位）" autocomplete="nickname" maxlength="32" required/>
                </div>
              </div>
              <div>
                <label class="sr-only" for="reg-email">邮箱</label>
                <div class="field-shell">
                  <span class="field-icon" aria-hidden="true">
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                      <rect x="3" y="5" width="18" height="14" rx="2"></rect>
                      <path d="M3 7l9 7l9-7"></path>
                    </svg>
                  </span>
                  <input type="email" name="email" id="reg-email" placeholder="请输入邮箱" data-ph="请输入邮箱" autocomplete="email" required/>
                </div>
              </div>
              <div>
                <label class="sr-only" for="reg-password">密码</label>
                <div class="field-shell">
                  <span class="field-icon field-pass" aria-hidden="true">
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                      <rect x="5.5" y="10.5" width="13" height="9" rx="2"></rect>
                      <path d="M8.5 10.5V8.4a3.5 3.5 0 1 1 7 0v2.1"></path>
                    </svg>
                  </span>
                  <input type="password" name="password" id="reg-password" placeholder="请设置密码（至少 8 位）" data-ph="请设置密码（至少 8 位）" autocomplete="new-password" required/>
                </div>
              </div>
              <div>
                <label class="sr-only" for="reg-confirm">确认密码</label>
                <div class="field-shell">
                  <span class="field-icon field-pass" aria-hidden="true">
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                      <rect x="5.5" y="10.5" width="13" height="9" rx="2"></rect>
                      <path d="M8.5 10.5V8.4a3.5 3.5 0 1 1 7 0v2.1"></path>
                    </svg>
                  </span>
                  <input type="password" name="confirm_password" id="reg-confirm" placeholder="请再次输入密码" data-ph="请再次输入密码" autocomplete="new-password" required/>
                </div>
              </div>
              <div>
                <label class="sr-only" for="reg-realname">真实姓名</label>
                <div class="field-shell">
                  <span class="field-icon" aria-hidden="true">
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                      <path d="M4 5h16v4H4zM4 11h16v8H4z"></path>
                    </svg>
                  </span>
                  <input type="text" name="real_name" id="reg-realname" placeholder="真实姓名（可选）" data-ph="真实姓名（可选）" autocomplete="name"/>
                </div>
              </div>
              <div>
                <label class="sr-only" for="reg-phone">联系方式</label>
                <div class="field-shell">
                  <span class="field-icon" aria-hidden="true">
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                      <path d="M6.6 3h3l1.5 4l-2 1.5a10 10 0 0 0 6.4 6.4l1.5-2l4 1.5v3a2 2 0 0 1-2 2A16 16 0 0 1 4 6.6a2 2 0 0 1 2.6-3.6z"></path>
                    </svg>
                  </span>
                  <input type="text" name="phone" id="reg-phone" placeholder="联系方式（可选）" data-ph="联系方式（可选）" autocomplete="tel"/>
                </div>
              </div>
              {error_html}
              <button type="submit" data-i18n="注册并登录">注册并登录</button>
            </form>
          </div>"""


def _render_legacy_mock_page(redirect: str, error: Optional[str]) -> str:
    """Legacy Mock SSO login page: MOCK_USERS account dropdown + password input, without the register tab / floating debug panel.

    Kept as the /mock-sso/login entry in mock mode to preserve the "previous state", decoupled from the new /login.
    """
    error_html = f'<div class="error-row" id="formError" role="alert"{" hidden" if not error else ""}>{error or ""}</div>'
    first = MOCK_USERS[0] if MOCK_USERS else {"username": "", "email": ""}
    option_items = "".join(
        f'<li><button type="button" class="username-option{" active" if i == 0 else ""}" '
        f'data-username="{u["username"]}" data-label="{u["username"]} ({u["email"]})" '
        f'role="option" aria-selected="{"true" if i == 0 else "false"}">'
        f'{u["username"]} ({u["email"]})</button></li>'
        for i, u in enumerate(MOCK_USERS)
    )
    brand = get_branding_info()
    brand_name = brand["product_name"]

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>统一身份认证登录 · Mock</title>
  <link rel="preload" as="image" href="/home/mock-sso-bg-original.png"/>
  <style>
    :root {{ --primary:#126dff; --primary-hover:#3c87ff; --text:#262626; --muted:#808080; --border:#d8dbe2; --danger:#fc5d5d; }}
    * {{ box-sizing:border-box; }}
    html, body {{ height:100%; }}
    body {{
      margin:0;
      font-family:"PingFang SC","Microsoft YaHei","微软雅黑",sans-serif;
      color:var(--text);
      background:
        linear-gradient(128deg, rgba(219,233,255,0.18) 0%, rgba(237,244,255,0.10) 36%, rgba(249,251,255,0.06) 74%, rgba(244,248,255,0.16) 100%),
        url('/home/mock-sso-bg-original.png') center center / cover no-repeat,
        linear-gradient(180deg, #f7fbff 0%, #edf5ff 100%);
      background-attachment: fixed;
    }}
    .page {{ position:relative; min-height:100vh; overflow:hidden; }}
    .brand {{ position:relative; z-index:1; display:flex; align-items:center; padding:46px 56px 0; }}
    .brand-link {{ display:inline-flex; align-items:center; text-decoration:none; color:inherit; }}
    .brand-logo {{ width:280px; max-width:calc(100vw - 112px); height:auto; display:block; }}
    .main {{
      position:relative; z-index:1;
      display:grid; grid-template-columns:minmax(0,1.08fr) minmax(380px,497px);
      gap:64px; align-items:center;
      min-height:calc(100vh - 140px); padding:24px 88px 96px;
    }}
    .visual {{ position:relative; min-height:640px; }}
    .scene-tag {{
      position:absolute; display:inline-flex; align-items:center; justify-content:center;
      min-width:62px; height:26px; padding:0 12px;
      background:rgba(255,255,255,0.92); border:1px solid rgba(18,109,255,0.34);
      border-radius:999px; box-shadow:0 8px 18px rgba(37,99,235,0.08);
      color:#4c4c4c; font-size:12px;
    }}
    .tag-ai {{ top:8.8%; left:17%; }}
    .tag-agent {{ top:26.8%; left:56%; }}
    .tag-kb {{ top:64%; left:32%; }}
    .panel {{ display:flex; align-items:center; justify-content:center; }}
    .card {{
      width:100%; min-height:420px; padding:70px 40px 40px;
      background:linear-gradient(180deg, rgba(255,255,255,0.93) 0%, rgba(255,255,255,0.88) 100%);
      border:1px solid rgba(255,255,255,0.72); border-radius:14px;
      box-shadow:0 18px 50px rgba(149,171,209,0.18);
      backdrop-filter:blur(10px);
    }}
    .mock-badge {{
      display:inline-flex; align-items:center; justify-content:center;
      margin:0 auto 10px; padding:3px 10px; border-radius:999px;
      background:rgba(255,243,224,0.86); color:#e67626; font-size:11px; letter-spacing:.04em;
    }}
    .title {{ margin:0 0 8px; font-size:28px; font-weight:600; text-align:center; color:var(--text); }}
    .hint {{ margin:0 0 22px; color:var(--muted); font-size:14px; text-align:center; }}
    form {{ display:flex; flex-direction:column; gap:18px; }}
    .sr-only {{ position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0,0,0,0); }}
    .field-shell {{
      position:relative; display:flex; align-items:center; height:46px;
      border:1px solid rgba(18,109,255,0.18); border-radius:8px;
      background:rgba(255,255,255,0.96);
      transition:border-color .2s ease, box-shadow .2s ease;
    }}
    .field-shell.username-shell {{ overflow:visible; }}
    .field-shell:focus-within {{ border-color:var(--primary); box-shadow:0 0 0 2px rgba(18,109,255,0.08); }}
    .field-icon {{ flex:0 0 36px; width:36px; height:100%; display:flex; align-items:center; justify-content:center; color:#c3c7cf; }}
    .field-icon svg {{ width:18px; height:18px; stroke:currentColor; fill:none; stroke-width:1.75; stroke-linecap:round; stroke-linejoin:round; }}
    .username-field {{ position:relative; flex:1; height:100%; }}
    .username-input {{
      width:100%; height:100%; display:flex; align-items:center; padding:0 38px 0 4px;
      font-size:14px; color:var(--text); border:none; background:transparent; cursor:pointer; user-select:none; text-align:left;
    }}
    .username-caret {{
      position:absolute; right:16px; top:50%; width:12px; height:12px;
      background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12' fill='none'%3E%3Cpath d='M2.5 4.5 6 8l3.5-3.5' stroke='%23333' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") center / 12px 12px no-repeat;
      transform:translateY(-50%);
      transition:transform .24s cubic-bezier(.22,1,.36,1);
      opacity:.82; pointer-events:none;
    }}
    .username-field.open .username-caret {{ transform:translateY(-50%) rotate(180deg); }}
    .username-value {{ display:block; width:100%; overflow:hidden; white-space:nowrap; text-overflow:ellipsis; }}
    .username-menu {{
      position:absolute; left:-1px; right:-1px; top:calc(100% + 8px);
      margin:0; padding:8px; list-style:none;
      border:1px solid rgba(18,109,255,0.14); border-radius:10px;
      background:rgba(255,255,255,0.98);
      box-shadow:0 16px 34px rgba(86,113,164,0.16);
      backdrop-filter:blur(12px); display:none; z-index:10;
    }}
    .username-field.open .username-menu {{ display:block; }}
    .username-option {{
      display:flex; align-items:center; width:100%; min-height:40px; padding:0 14px;
      border:none; border-radius:8px; background:transparent; color:#243248; font-size:14px;
      text-align:left; cursor:pointer; transition:background .18s ease, color .18s ease;
    }}
    .username-option:hover {{ background:rgba(18,109,255,0.08); color:#1f4fd9; }}
    .username-option.active {{ background:rgba(18,109,255,0.10); color:#1650e6; font-weight:600; }}
    input[type=password] {{
      display:block; width:100%; height:100%; padding:0 16px 0 4px;
      font-size:14px; color:var(--text); border:none; background:transparent; outline:none;
    }}
    .error-row {{ margin-top:-4px; color:var(--danger); font-size:13px; line-height:1.5; }}
    .error-row[hidden] {{ display:none; }}
    button[type=submit] {{
      display:block; width:100%; height:52px; margin-top:6px;
      font-size:18px; font-weight:600; letter-spacing:.08em;
      color:#fff; background:var(--primary); border:none; border-radius:8px;
      box-shadow:0 14px 24px rgba(18,109,255,0.18); cursor:pointer;
      transition:background .2s ease, transform .2s ease;
    }}
    button[type=submit]:hover {{ background:var(--primary-hover); transform:translateY(-1px); }}
    .footer {{
      position:absolute; left:50%; bottom:22px; z-index:1; transform:translateX(-50%);
      width:min(90vw,720px); color:rgba(92,108,132,0.72); font-size:12px; text-align:center;
    }}
    @media (max-width: 960px) {{
      .brand {{ padding:28px 24px 0; }}
      .brand-logo {{ width:240px; max-width:calc(100vw - 48px); }}
      .main {{ grid-template-columns:1fr; gap:24px; min-height:auto; padding:12px 20px 108px; }}
      .visual {{ display:none; }}
      .card {{ min-height:unset; padding:40px 22px 28px; }}
      .title {{ font-size:24px; }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <header class="brand">
      <a class="brand-link" href="/mock-sso/login?redirect={redirect}">
        <img class="brand-logo" src="/home/hugagentos-logo.png" alt="{brand_name}"/>
      </a>
    </header>
    <main class="main">
      <section class="visual" aria-hidden="true">
        <div class="scene-tag tag-ai">AI 问答</div>
        <div class="scene-tag tag-agent">智能体</div>
        <div class="scene-tag tag-kb">知识库</div>
      </section>
      <section class="panel">
        <div class="card">
          <div style="text-align:center"><span class="mock-badge">Mock SSO</span></div>
          <h1 class="title">统一身份认证登录</h1>
          <p class="hint">开发测试环境，请选择账号并输入密码</p>
          <form method="POST" action="/mock-sso/login" id="loginForm" novalidate>
            <input type="hidden" name="redirect" value="{redirect}"/>
            <div>
              <label class="sr-only" for="username">账号</label>
              <div class="field-shell username-shell">
                <span class="field-icon" aria-hidden="true">
                  <svg viewBox="0 0 24 24"><path d="M12 12a4.2 4.2 0 1 0 0-8.4a4.2 4.2 0 0 0 0 8.4Z"/><path d="M4.8 19.2a7.2 7.2 0 0 1 14.4 0"/></svg>
                </span>
                <div class="username-field" id="usernameField">
                  <input type="hidden" name="username" id="username" value="{first['username']}"/>
                  <button type="button" class="username-input" id="usernameTrigger" aria-haspopup="listbox" aria-expanded="false">
                    <span class="username-value" id="usernameValue">{first['username']} ({first['email']})</span>
                    <span class="username-caret" aria-hidden="true"></span>
                  </button>
                  <ul class="username-menu" id="usernameMenu" role="listbox">{option_items}</ul>
                </div>
              </div>
            </div>
            <div>
              <label class="sr-only" for="password">密码</label>
              <div class="field-shell">
                <span class="field-icon" aria-hidden="true">
                  <svg viewBox="0 0 24 24"><rect x="5.5" y="10.5" width="13" height="9" rx="2"/><path d="M8.5 10.5V8.4a3.5 3.5 0 1 1 7 0v2.1"/></svg>
                </span>
                <input type="password" name="password" id="password" placeholder="请输入密码" autocomplete="current-password" required/>
              </div>
            </div>
            {error_html}
            <button type="submit">登录</button>
          </form>
        </div>
      </section>
    </main>
    <footer class="footer">致力于构建面向未来的组织级AI生产力平台</footer>
  </div>
  <script>
    (function() {{
      const field = document.getElementById('usernameField');
      const trigger = document.getElementById('usernameTrigger');
      const menu = document.getElementById('usernameMenu');
      const valueEl = document.getElementById('usernameValue');
      const input = document.getElementById('username');
      const form = document.getElementById('loginForm');
      const password = document.getElementById('password');
      const formError = document.getElementById('formError');
      if (!field || !trigger || !menu || !valueEl || !input || !form || !password) return;
      const options = Array.from(menu.querySelectorAll('.username-option'));
      const setOpen = (open) => {{ field.classList.toggle('open', open); trigger.setAttribute('aria-expanded', open ? 'true' : 'false'); }};
      trigger.addEventListener('click', () => setOpen(!field.classList.contains('open')));
      options.forEach((option) => {{
        option.addEventListener('click', () => {{
          const u = option.getAttribute('data-username') || '';
          const l = option.getAttribute('data-label') || '';
          input.value = u; valueEl.textContent = l;
          options.forEach(o => {{ o.classList.toggle('active', o === option); o.setAttribute('aria-selected', o === option ? 'true' : 'false'); }});
          setOpen(false);
        }});
      }});
      form.addEventListener('submit', (e) => {{
        if (!password.value.trim()) {{
          e.preventDefault();
          if (formError) {{ formError.textContent = '请输入密码'; formError.hidden = false; }}
          password.focus();
        }}
      }});
      document.addEventListener('click', (e) => {{ if (!field.contains(e.target)) setOpen(false); }});
      document.addEventListener('keydown', (e) => {{ if (e.key === 'Escape') setOpen(false); }});
    }})();
  </script>
</body>
</html>"""


@router.get("/login", summary="Mock SSO 登录页（旧版：下拉账号 + 密码）")
async def mock_legacy_login_page(
    request: Request,
    redirect: str = Query("/"),
    auto: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
):
    """/mock-sso/login 专用：保留旧版 Mock 登录页样式（下拉 + 密码）。"""
    if auto is not None and _mock_account_shortcuts_enabled():
        try:
            idx = int(auto)
            user = MOCK_USERS[idx]
        except (ValueError, IndexError):
            user = MOCK_USERS[0]
        ticket = _generate_ticket(_user_info_without_password(user))
        target = _build_redirect_target(request, redirect, ticket)
        return RedirectResponse(url=target, status_code=302)
    # local mode no longer exposes the Mock dropdown-account page: everything converges on /login, so no entry point bounces back to the mock page.
    # In explicit non-CE mock mode, the legacy dropdown page and its auto-login
    # shortcut remain available for development.
    try:
        from core.config.settings import settings as _settings

        if _settings.sso.effective_login_mode == "local":
            login_target = (
                f"/login?redirect={_encode_query(redirect)}"
                if redirect and redirect != "/"
                else "/login"
            )
            return RedirectResponse(url=login_target, status_code=302)
    except Exception:
        pass
    return HTMLResponse(content=_render_legacy_mock_page(redirect, error))


# ── Endpoints ────────────────────────────────────────────────────────────


async def mock_login_page(
    request: Request,
    redirect: str = Query("/", description="登录成功后前端跳转路径"),
    auto: Optional[str] = Query(None, description="自动登录的用户序号 (0/1/2)，跳过密码验证"),
    error: Optional[str] = Query(None, description="登录失败时的错误提示"),
    tab: Optional[str] = Query(None, description="默认显示的 Tab：login / register"),
    reg_error: Optional[str] = Query(None, description="注册失败时的错误提示"),
):
    """Render a login page with tabs for login/register (local accounts) and a dev-only Mock account entry."""
    # Auto-login shortcut (for scripts/tests, skips the password)
    if auto is not None and _mock_account_shortcuts_enabled():
        try:
            idx = int(auto)
            user = MOCK_USERS[idx]
        except (ValueError, IndexError):
            user = MOCK_USERS[0]
        ticket = _generate_ticket(_user_info_without_password(user))
        target = _build_redirect_target(request, redirect, ticket)
        return RedirectResponse(url=target, status_code=302)

    # Deferred import to avoid a module-level dependency on settings / the database
    try:
        from core.config.settings import settings as _settings

        local_enabled = bool(_settings.auth.local_enabled)
        default_lang = "en" if _settings.edition.edition == "ce" else "zh-CN"
    except Exception:
        local_enabled = True
        default_lang = "zh-CN"

    # The registration entry is gated by two switches: the local-account master switch (env) + the page_config registration switch (operators configure it under /config).
    show_register = local_enabled and is_register_allowed()

    active_tab = "register" if (tab == "register" and show_register) else "login"
    login_active = "active" if active_tab == "login" else ""
    register_active = "active" if active_tab == "register" else ""

    # Use relative URLs so both mount points, `/mock-sso/login` and `/login`, POST correctly
    login_action = "login"
    register_action = "register"

    login_error_html = f'<div class="error-row" id="loginError" role="alert"{" hidden" if not error else ""}>{error or ""}</div>'
    register_error_html = f'<div class="error-row" id="registerError" role="alert"{" hidden" if not reg_error else ""}>{reg_error or ""}</div>'
    register_tab_button = (
        f'<button type="button" class="tab-btn {register_active}" data-tab="register" data-i18n="注册">注册</button>'
        if show_register
        else ""
    )
    # When registration is disabled, hide the entire tab bar — only the login form remains, instead of showing a lone "Login" tab.
    login_tab_button = f'<button type="button" class="tab-btn {login_active}" data-tab="login" role="tab" data-i18n="登录">登录</button>'
    tabs_html = (
        f'<div class="tabs" role="tablist">{login_tab_button}{register_tab_button}</div>'
        if show_register
        else ""
    )
    register_form_html = (
        _render_register_form(redirect, register_error_html, register_action)
        if show_register
        else ""
    )
    mock_hint_html = ""
    brand = get_branding_info()
    brand_name = brand["product_name"]

    html = f"""<!DOCTYPE html>
<html lang="{default_lang if default_lang == 'en' else 'zh-CN'}">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>{brand_name}</title>
  <link rel="preload" as="image" href="/home/mock-sso-bg-original.png"/>
  <style>
    :root {{
      --primary:#126dff;
      --primary-hover:#3c87ff;
      --text:#262626;
      --muted:#808080;
      --border:#d8dbe2;
      --danger:#fc5d5d;
    }}
    * {{ box-sizing:border-box; }}
    html, body {{ height:100%; }}
    body {{
      margin:0;
      font-family:"PingFang SC","Microsoft YaHei","微软雅黑",sans-serif;
      color:var(--text);
      background:
        linear-gradient(128deg, rgba(219, 233, 255, 0.18) 0%, rgba(237, 244, 255, 0.10) 36%, rgba(249, 251, 255, 0.06) 74%, rgba(244, 248, 255, 0.16) 100%),
        url('/home/mock-sso-bg-original.png') center center / cover no-repeat,
        linear-gradient(180deg, #f7fbff 0%, #edf5ff 100%);
      background-attachment: fixed;
    }}
    .page {{
      position:relative;
      min-height:100vh;
      overflow:hidden;
    }}
    .page::before {{
      content:"";
      position:absolute;
      inset:0;
      background:
        radial-gradient(circle at 18% 18%, rgba(18,109,255,0.08) 0, rgba(18,109,255,0) 24%),
        radial-gradient(circle at 88% 12%, rgba(164,197,255,0.14) 0, rgba(164,197,255,0) 22%);
      pointer-events:none;
    }}
    .brand {{
      position:relative;
      z-index:1;
      display:flex;
      align-items:center;
      padding:46px 56px 0;
    }}
    .brand-link {{
      display:inline-flex;
      align-items:center;
      gap:12px;
      text-decoration:none;
      color:inherit;
      border-radius:12px;
      transition:opacity .18s ease, transform .18s ease;
    }}
    .brand-link:hover {{
      opacity:.9;
      transform:translateY(-1px);
    }}
    .brand-logo {{
      width:280px;
      max-width:calc(100vw - 112px);
      height:auto;
      display:block;
    }}
    .main {{
      position:relative;
      z-index:1;
      display:grid;
      grid-template-columns:minmax(0, 1.08fr) minmax(380px, 497px);
      gap:64px;
      align-items:center;
      min-height:calc(100vh - 140px);
      padding:24px 88px 96px;
    }}
    .visual {{
      position:relative;
      min-height:640px;
    }}
    .scene-tag {{
      position:absolute;
      display:inline-flex;
      align-items:center;
      justify-content:center;
      min-width:62px;
      height:26px;
      padding:0 12px;
      background:rgba(255,255,255,0.92);
      border:1px solid rgba(18,109,255,0.34);
      border-radius:999px;
      box-shadow:0 8px 18px rgba(37,99,235,0.08);
      color:#4c4c4c;
      font-size:12px;
      line-height:1;
    }}
    .scene-tag::after {{
      content:"";
      position:absolute;
      left:50%;
      top:100%;
      width:1px;
      height:18px;
      background:linear-gradient(180deg, rgba(18,109,255,0.4) 0%, rgba(18,109,255,0) 100%);
      transform:translateX(-50%);
    }}
    .tag-ai {{ top:8.8%; left:17%; }}
    .tag-agent {{ top:26.8%; left:56%; }}
    .tag-kb {{ top:64%; left:32%; }}
    .panel {{
      display:flex;
      align-items:center;
      justify-content:center;
    }}
    .card {{
      width:100%;
      min-height:516px;
      padding:78px 40px 40px;
      background:linear-gradient(180deg, rgba(255,255,255,0.93) 0%, rgba(255,255,255,0.88) 100%);
      border:1px solid rgba(255,255,255,0.72);
      border-radius:14px;
      box-shadow:0 18px 50px rgba(149,171,209,0.18), inset 0 1px 0 rgba(255,255,255,0.65);
      backdrop-filter:blur(10px);
    }}
    .title {{
      margin:0 0 44px;
      font-size:28px;
      line-height:1.2;
      font-weight:600;
      text-align:center;
      color:var(--text);
    }}
    .hint {{
      margin:0 0 22px;
      color:var(--muted);
      font-size:14px;
      text-align:center;
    }}
    form {{ display:flex; flex-direction:column; gap:18px; }}
    .sr-only {{
      position:absolute;
      width:1px;
      height:1px;
      padding:0;
      margin:-1px;
      overflow:hidden;
      clip:rect(0,0,0,0);
      white-space:nowrap;
      border:0;
    }}
    .field-shell {{
      position:relative;
      display:flex;
      align-items:center;
      height:46px;
      border:1px solid rgba(18,109,255,0.18);
      border-radius:8px;
      background:rgba(255,255,255,0.96);
      transition:border-color .2s ease, box-shadow .2s ease;
      overflow:hidden;
    }}
    .field-shell.username-shell {{
      overflow:visible;
    }}
    .field-shell:focus-within {{
      border-color:var(--primary);
      box-shadow:0 0 0 2px rgba(18,109,255,0.08);
    }}
    .field-icon {{
      flex:0 0 36px;
      width:36px;
      height:100%;
      display:flex;
      align-items:center;
      justify-content:center;
      color:#c3c7cf;
      opacity:.96;
    }}
    .field-icon svg {{
      width:18px;
      height:18px;
      display:block;
      stroke:currentColor;
      fill:none;
      stroke-width:1.75;
      stroke-linecap:round;
      stroke-linejoin:round;
    }}
    .field-user svg {{
      width:19px;
      height:19px;
    }}
    .field-pass svg {{
      width:18px;
      height:18px;
    }}
    .username-field {{
      position:relative;
      flex:1;
      height:100%;
    }}
    .username-input {{
      width:100%;
      height:100%;
      display:flex;
      align-items:center;
      justify-content:flex-start;
      padding:0 38px 0 4px;
      font-size:14px;
      color:var(--text);
      border:none;
      background:transparent;
      cursor:pointer;
      user-select:none;
      position:relative;
      text-align:left;
    }}
    .username-caret {{
      position:absolute;
      right:16px;
      top:50%;
      width:12px;
      height:12px;
      background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12' fill='none'%3E%3Cpath d='M2.5 4.5 6 8l3.5-3.5' stroke='%23333' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") center / 12px 12px no-repeat;
      transform:translateY(-50%);
      transition:transform .24s cubic-bezier(.22, 1, .36, 1), opacity .2s ease;
      opacity:.82;
      pointer-events:none;
      transform-origin:center;
    }}
    .username-field.open .username-caret {{
      transform:translateY(-50%) rotate(180deg);
    }}
    .username-value {{
      display:block;
      width:100%;
      overflow:hidden;
      white-space:nowrap;
      text-overflow:ellipsis;
    }}
    .username-menu {{
      position:absolute;
      left:-1px;
      right:-1px;
      top:calc(100% + 8px);
      margin:0;
      padding:8px;
      list-style:none;
      border:1px solid rgba(18,109,255,0.14);
      border-radius:10px;
      background:rgba(255,255,255,0.98);
      box-shadow:0 16px 34px rgba(86, 113, 164, 0.16), 0 2px 10px rgba(110, 135, 179, 0.08);
      backdrop-filter:blur(12px);
      display:none;
      z-index:10;
    }}
    .username-field.open .username-menu {{
      display:block;
    }}
    .username-option {{
      display:flex;
      align-items:center;
      width:100%;
      min-height:40px;
      padding:0 14px;
      border:none;
      border-radius:8px;
      background:transparent;
      color:#243248;
      font-size:14px;
      line-height:1.45;
      text-align:left;
      cursor:pointer;
      transition:background .18s ease, color .18s ease;
    }}
    .username-option:hover {{
      background:rgba(18,109,255,0.08);
      color:#1f4fd9;
    }}
    .username-option.active {{
      background:linear-gradient(180deg, rgba(18,109,255,0.12) 0%, rgba(18,109,255,0.08) 100%);
      color:#1650e6;
      font-weight:600;
      margin:2px 0;
    }}
    .username-option:focus-visible,
    .username-input:focus-visible {{
      outline:none;
      box-shadow:0 0 0 2px rgba(18,109,255,0.12);
    }}
    input[type=password] {{
      display:block;
      width:100%;
      height:100%;
      padding:0 16px 0 4px;
      font-size:14px;
      color:var(--text);
      border:none;
      border-radius:0;
      background:transparent;
      outline:none;
      box-shadow:none;
    }}
    .error-row {{
      margin-top:-4px;
      margin-bottom:4px;
      color:var(--danger);
      font-size:13px;
      line-height:1.5;
    }}
    .error-row[hidden] {{
      display:none;
    }}
    button[type=submit] {{
      display:block;
      width:100%;
      height:52px;
      margin-top:6px;
      font-size:18px;
      font-weight:600;
      letter-spacing:.08em;
      color:#fff;
      background:var(--primary);
      border:none;
      border-radius:8px;
      box-shadow:0 14px 24px rgba(18,109,255,0.18);
      cursor:pointer;
      transition:background .2s ease, transform .2s ease;
    }}
    button[type=submit]:hover {{
      background:var(--primary-hover);
      transform:translateY(-1px);
    }}
    .footer {{
      position:absolute;
      left:50%;
      bottom:22px;
      z-index:1;
      width:min(90vw, 720px);
      color:rgba(92, 108, 132, 0.72);
      font-size:12px;
      font-weight:400;
      line-height:1.8;
      letter-spacing:.02em;
      text-align:center;
      transform:translateX(-50%);
      transition:color .24s ease, opacity .24s ease, transform .24s ease;
      cursor:default;
    }}
    .footer::after {{
      content:"";
      position:absolute;
      left:50%;
      bottom:-4px;
      width:140px;
      height:1px;
      background:linear-gradient(90deg, rgba(18,109,255,0) 0%, rgba(18,109,255,0.18) 50%, rgba(18,109,255,0) 100%);
      opacity:0;
      transform:translateX(-50%);
      transition:opacity .24s ease, width .28s ease;
      pointer-events:none;
    }}
    .footer:hover {{
      color:rgba(63, 84, 119, 0.88);
      transform:translateX(-50%) translateY(-1px);
    }}
    .footer:hover::after {{
      opacity:1;
      width:184px;
    }}
    .tabs {{
      display:flex;
      gap:4px;
      margin:0 0 24px;
      padding:4px;
      background:rgba(18,109,255,0.06);
      border-radius:10px;
    }}
    .tab-btn {{
      flex:1;
      display:flex;
      align-items:center;
      justify-content:center;
      height:38px;
      padding:0 12px;
      font-size:14px;
      font-weight:500;
      color:var(--muted);
      background:transparent;
      border:none;
      border-radius:8px;
      cursor:pointer;
      transition:background .2s ease, color .2s ease, box-shadow .2s ease;
    }}
    .tab-btn.active {{
      color:var(--primary);
      background:#fff;
      box-shadow:0 2px 6px rgba(18,109,255,0.08);
    }}
    .form-panel {{ display:none; }}

    /* ── 语言切换按钮（右上角，与前端共用 jx_lang） ─────────── */
    .lang-toggle {{
      position:fixed;
      top:28px;
      right:32px;
      z-index:6;
      display:inline-flex;
      align-items:center;
      height:34px;
      padding:0 16px;
      background:rgba(255,255,255,0.72);
      border:1px solid rgba(18,109,255,0.18);
      border-radius:999px;
      color:rgba(71,92,128,0.85);
      font-size:13px;
      letter-spacing:.02em;
      cursor:pointer;
      backdrop-filter:blur(12px);
      -webkit-backdrop-filter:blur(12px);
      box-shadow:0 6px 18px rgba(49,77,131,0.08);
      transition:color .2s ease, border-color .2s ease, background .2s ease, transform .2s ease;
    }}
    .lang-toggle:hover {{
      color:var(--primary);
      border-color:rgba(18,109,255,0.36);
      background:rgba(255,255,255,0.94);
      transform:translateY(-1px);
    }}
    .form-panel.active {{ display:flex; flex-direction:column; }}

    /* ── 开发调试入口（右下角浮动，不进入表单布局） ─────────── */
    .dev-dock {{
      position:fixed;
      right:28px;
      bottom:28px;
      z-index:5;
      font-family:inherit;
    }}
    .dev-dock-toggle {{
      display:inline-flex;
      align-items:center;
      gap:8px;
      height:34px;
      padding:0 14px 0 12px;
      background:rgba(255,255,255,0.72);
      border:1px solid rgba(18,109,255,0.18);
      border-radius:999px;
      color:rgba(71,92,128,0.78);
      font-size:12px;
      letter-spacing:.02em;
      cursor:pointer;
      backdrop-filter:blur(12px);
      -webkit-backdrop-filter:blur(12px);
      box-shadow:0 6px 18px rgba(49,77,131,0.08);
      transition:color .2s ease, border-color .2s ease, transform .2s ease, box-shadow .2s ease, background .2s ease;
    }}
    .dev-dock-toggle:hover,
    .dev-dock.open .dev-dock-toggle {{
      color:var(--primary);
      border-color:rgba(18,109,255,0.36);
      background:rgba(255,255,255,0.94);
      transform:translateY(-1px);
      box-shadow:0 10px 24px rgba(18,109,255,0.14);
    }}
    .dev-dock-dot {{
      width:6px;
      height:6px;
      border-radius:50%;
      background:#f59e0b;
      box-shadow:0 0 0 3px rgba(245,158,11,0.18);
      animation:dev-pulse 2.4s ease-in-out infinite;
    }}
    @keyframes dev-pulse {{
      0%, 100% {{ box-shadow:0 0 0 3px rgba(245,158,11,0.18); }}
      50%      {{ box-shadow:0 0 0 6px rgba(245,158,11,0.05); }}
    }}
    .dev-dock-label {{ font-weight:500; }}
    .dev-dock-caret {{
      width:10px;
      height:10px;
      transition:transform .24s cubic-bezier(.22,1,.36,1);
      opacity:.6;
    }}
    .dev-dock.open .dev-dock-caret {{ transform:rotate(-180deg); }}

    .dev-dock-panel {{
      position:absolute;
      right:0;
      bottom:calc(100% + 10px);
      min-width:244px;
      max-width:300px;
      padding:14px;
      background:rgba(255,255,255,0.96);
      border:1px solid rgba(18,109,255,0.14);
      border-radius:14px;
      box-shadow:0 22px 48px rgba(49,77,131,0.14), 0 4px 14px rgba(49,77,131,0.08);
      backdrop-filter:blur(16px);
      -webkit-backdrop-filter:blur(16px);
      opacity:0;
      transform:translateY(6px) scale(0.98);
      pointer-events:none;
      transition:opacity .22s ease, transform .22s cubic-bezier(.22,1,.36,1);
    }}
    .dev-dock.open .dev-dock-panel {{
      opacity:1;
      transform:translateY(0) scale(1);
      pointer-events:auto;
    }}
    .dev-dock-panel::after {{
      content:"";
      position:absolute;
      right:24px;
      bottom:-6px;
      width:12px;
      height:12px;
      background:rgba(255,255,255,0.96);
      border-right:1px solid rgba(18,109,255,0.14);
      border-bottom:1px solid rgba(18,109,255,0.14);
      transform:rotate(45deg);
      border-bottom-right-radius:2px;
    }}
    .dev-dock-head {{
      display:flex;
      align-items:baseline;
      justify-content:space-between;
      padding:2px 4px 10px;
      margin-bottom:8px;
      border-bottom:1px solid rgba(18,109,255,0.08);
    }}
    .dev-dock-title {{
      font-size:12px;
      font-weight:600;
      color:#1f2937;
      letter-spacing:.02em;
    }}
    .dev-dock-sub {{
      font-size:10px;
      color:rgba(107,114,128,0.8);
      letter-spacing:.04em;
    }}
    .dev-dock-list {{
      display:grid;
      grid-template-columns:repeat(2, 1fr);
      gap:6px;
    }}
    .dev-chip {{
      display:flex;
      flex-direction:column;
      align-items:flex-start;
      justify-content:center;
      gap:2px;
      padding:8px 10px;
      background:rgba(246,249,255,0.85);
      border:1px solid rgba(18,109,255,0.08);
      border-radius:8px;
      cursor:pointer;
      text-align:left;
      transition:background .18s ease, border-color .18s ease, transform .12s ease;
    }}
    .dev-chip:hover {{
      background:rgba(18,109,255,0.08);
      border-color:rgba(18,109,255,0.28);
      transform:translateY(-1px);
    }}
    .dev-chip-name {{
      font-size:13px;
      font-weight:600;
      color:#1f2937;
      line-height:1.2;
    }}
    .dev-chip-hint {{
      font-size:10px;
      color:rgba(107,114,128,0.75);
      letter-spacing:.02em;
    }}
    .dev-chip:hover .dev-chip-hint {{ color:var(--primary); }}
    input[type=text] {{
      display:block;
      width:100%;
      height:100%;
      padding:0 16px 0 4px;
      font-size:14px;
      color:var(--text);
      border:none;
      border-radius:0;
      background:transparent;
      outline:none;
      box-shadow:none;
    }}
    @media (max-width: 960px) {{
      .brand {{ padding:28px 24px 0; }}
      .brand-logo {{ width:240px; max-width:calc(100vw - 48px); }}
      .main {{
        grid-template-columns:1fr;
        gap:24px;
        min-height:auto;
        padding:12px 20px 108px;
      }}
      .visual {{ display:none; }}
      .card {{ min-height:unset; padding:40px 22px 28px; }}
      .title {{ margin-bottom:28px; font-size:24px; }}
      .dev-dock {{ right:16px; bottom:16px; }}
      .lang-toggle {{ top:16px; right:16px; }}
      .dev-dock-panel {{ min-width:220px; }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <header class="brand">
      <a class="brand-link" href="/login?redirect={redirect}">
        <img class="brand-logo" src="/home/hugagentos-logo.png" alt="{brand_name}" />
      </a>
    </header>
    <main class="main">
      <section class="visual" aria-hidden="true">
        <div class="scene-tag tag-ai" data-i18n="AI 问答">AI 问答</div>
        <div class="scene-tag tag-agent" data-i18n="智能体">智能体</div>
        <div class="scene-tag tag-kb" data-i18n="知识库">知识库</div>
      </section>
      <section class="panel">
        <div class="card">
          <h1 class="title" data-i18n="统一身份认证登录">统一身份认证登录</h1>
          {tabs_html}
          <div class="form-panel {login_active}" id="panel-login">
            <form method="POST" action="{login_action}" id="loginForm" novalidate>
              <input type="hidden" name="redirect" value="{redirect}"/>
              <div>
                <label class="sr-only" for="username">账号</label>
                <div class="field-shell">
                  <span class="field-icon field-user" aria-hidden="true">
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                      <path d="M12 12a4.2 4.2 0 1 0 0-8.4a4.2 4.2 0 0 0 0 8.4Z"></path>
                      <path d="M4.8 19.2a7.2 7.2 0 0 1 14.4 0"></path>
                    </svg>
                  </span>
                  <input type="text" name="username" id="username" placeholder="请输入账号或邮箱" data-ph="请输入账号或邮箱" autocomplete="username" required/>
                </div>
              </div>
              <div>
                <label class="sr-only" for="password">密码</label>
                <div class="field-shell">
                  <span class="field-icon field-pass" aria-hidden="true">
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                      <rect x="5.5" y="10.5" width="13" height="9" rx="2"></rect>
                      <path d="M8.5 10.5V8.4a3.5 3.5 0 1 1 7 0v2.1"></path>
                    </svg>
                  </span>
                  <input type="password" name="password" id="password" placeholder="请输入密码" data-ph="请输入密码" autocomplete="current-password" required/>
                </div>
              </div>
              {login_error_html}
              <div style="display:flex;align-items:center;margin:2px 0 2px;">
                <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:13px;color:var(--muted);user-select:none;">
                  <input type="checkbox" name="remember" id="remember" value="1" checked style="width:15px;height:15px;margin:0;cursor:pointer;accent-color:var(--primary);"/>
                  <span data-i18n="记住登录状态">记住登录状态</span>
                </label>
              </div>
              <button type="submit" data-i18n="登录">登录</button>
            </form>
          </div>
          {register_form_html}
        </div>
      </section>
    </main>
    {mock_hint_html}
    <button type="button" class="lang-toggle" id="langToggle" aria-label="切换语言">English</button>
    <footer class="footer" data-i18n="致力于构建面向未来的组织级AI生产力平台">
      致力于构建面向未来的组织级AI生产力平台
    </footer>
  </div>
  <script>
    (function() {{
      /* ── i18n：与前端共用 localStorage('jx_lang')，文案客户端切换 ── */
      const I18N = {{
        '统一身份认证登录': 'Unified Identity Sign-in',
        'AI 问答': 'AI Q&A',
        '智能体': 'Agents',
        '知识库': 'Knowledge Base',
        '登录': 'Sign in',
        '注册': 'Sign up',
        '注册并登录': 'Sign up & Sign in',
        '请输入账号或邮箱': 'Enter username or email',
        '请输入密码': 'Enter password',
        '记住登录状态': 'Keep me signed in',
        '请输入注册码': 'Enter invite code',
        '账号（仅英文、数字、下划线，2-32 位）': 'Username (letters, digits, underscore; 2-32 chars)',
        '请输入用户名（昵称，最多 32 位）': 'Enter display name (max 32 chars)',
        '请输入邮箱': 'Enter email',
        '请设置密码（至少 8 位）': 'Set a password (min 8 chars)',
        '请再次输入密码': 'Confirm password',
        '真实姓名（可选）': 'Real name (optional)',
        '联系方式（可选）': 'Phone (optional)',
        '致力于构建面向未来的组织级AI生产力平台': 'Building a future-ready, organization-level AI productivity platform',
        '请输入账号和密码': 'Enter your username and password',
        '请填写注册码、账号、用户名、邮箱、密码': 'Fill in the invite code, username, display name, email and password',
        '账号只能包含英文、数字、下划线，长度 2-32 位': 'Username may only contain letters, digits and underscores (2-32 chars)',
        '用户名长度不能超过 32 位': 'Display name must be 32 characters or fewer',
        '请输入正确的邮箱格式': 'Enter a valid email address',
        '两次输入的密码不一致': 'Passwords do not match',
        '用户名或密码错误，请重试': 'Incorrect username or password. Please try again.',
        '注册已关闭': 'Registration is closed',
        '注册服务暂不可用': 'Registration service is temporarily unavailable',
        '注册失败，请稍后重试': 'Registration failed. Please try again later.',
        '注册失败': 'Registration failed',
        '账号不能为空': 'Username is required',
        '用户名不能为空': 'Display name is required',
        '邮箱不能为空': 'Email is required',
        '邮箱格式不正确': 'Invalid email format',
        '账号已被占用': 'Username is already taken',
        '邮箱已被使用': 'Email is already in use',
        '注册码无效': 'Invalid invite code',
        '注册码已被使用': 'Invite code has already been used',
        '账号或密码为空': 'Username or password is empty',
        '账号或密码错误': 'Incorrect username or password',
        '账号已被禁用': 'This account has been disabled',
        '账号待审核': 'This account is pending approval',
        '切换语言': 'Switch language',
      }};
      const LANG_KEY = 'jx_lang';
      function getLang() {{
        try {{
          const saved = localStorage.getItem(LANG_KEY);
          return saved === 'en' || saved === 'zh-CN' ? saved : '{default_lang}';
        }} catch (e) {{ return '{default_lang}'; }}
      }}
      let curLang = getLang();
      function tr(text) {{
        if (curLang !== 'en') return text;
        if (I18N[text]) return I18N[text];
        let m = text.match(/^密码长度至少 (\\d+) 位$/);
        if (m) return 'Password must be at least ' + m[1] + ' characters';
        m = text.match(/^注册失败：([\\s\\S]*)$/);
        if (m) return 'Registration failed: ' + m[1];
        return text;
      }}
      function applyLang() {{
        document.documentElement.lang = curLang === 'en' ? 'en' : 'zh';
        document.title = '{brand_name}';
        document.querySelectorAll('[data-i18n]').forEach((el) => {{ el.textContent = tr(el.dataset.i18n); }});
        document.querySelectorAll('input[data-ph]').forEach((el) => {{ el.placeholder = tr(el.dataset.ph); }});
        ['loginError', 'registerError'].forEach((id) => {{
          const el = document.getElementById(id);
          if (el && el.dataset.zh) el.textContent = tr(el.dataset.zh);
        }});
        const tg = document.getElementById('langToggle');
        if (tg) {{
          tg.textContent = curLang === 'en' ? '简体中文' : 'English';
          tg.setAttribute('aria-label', tr('切换语言'));
        }}
      }}
      function showError(el, zh) {{
        el.dataset.zh = zh;
        el.textContent = tr(zh);
        el.hidden = false;
      }}
      // 服务端回显的错误（query 注入）记下中文原文，便于切换时重译
      ['loginError', 'registerError'].forEach((id) => {{
        const el = document.getElementById(id);
        if (el && el.textContent.trim()) el.dataset.zh = el.textContent.trim();
      }});
      const langToggle = document.getElementById('langToggle');
      if (langToggle) {{
        langToggle.addEventListener('click', () => {{
          curLang = curLang === 'en' ? 'zh-CN' : 'en';
          try {{ localStorage.setItem(LANG_KEY, curLang); }} catch (e) {{ /* 隐私模式下仅本页生效 */ }}
          applyLang();
        }});
      }}
      applyLang();

      const tabs = Array.from(document.querySelectorAll('.tab-btn'));
      const panels = {{
        login: document.getElementById('panel-login'),
        register: document.getElementById('panel-register'),
      }};
      function switchTab(name) {{
        if (!panels[name]) return;
        tabs.forEach((btn) => {{
          btn.classList.toggle('active', btn.dataset.tab === name);
        }});
        Object.keys(panels).forEach((key) => {{
          if (!panels[key]) return;
          panels[key].classList.toggle('active', key === name);
        }});
      }}
      tabs.forEach((btn) => {{
        btn.addEventListener('click', () => switchTab(btn.dataset.tab));
      }});

      const loginForm = document.getElementById('loginForm');
      const loginError = document.getElementById('loginError');
      if (loginForm && loginError) {{
        loginForm.addEventListener('submit', (event) => {{
          const u = loginForm.elements['username'];
          const p = loginForm.elements['password'];
          if (!u.value.trim() || !p.value.trim()) {{
            event.preventDefault();
            showError(loginError, '请输入账号和密码');
          }}
        }});
      }}

      const registerForm = document.getElementById('registerForm');
      const registerError = document.getElementById('registerError');
      if (registerForm && registerError) {{
        const USERNAME_RE = /^[A-Za-z0-9_]{{2,32}}$/;
        const EMAIL_RE = /^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/;
        registerForm.addEventListener('submit', (event) => {{
          const code = registerForm.elements['code'].value.trim();
          const username = registerForm.elements['username'].value.trim();
          const nickname = (registerForm.elements['nickname']?.value || '').trim();
          const email = (registerForm.elements['email']?.value || '').trim();
          const password = registerForm.elements['password'].value;
          const confirm = registerForm.elements['confirm_password'].value;
          if (!code || !username || !nickname || !email || !password) {{
            event.preventDefault();
            showError(registerError, '请填写注册码、账号、用户名、邮箱、密码');
            return;
          }}
          if (!USERNAME_RE.test(username)) {{
            event.preventDefault();
            showError(registerError, '账号只能包含英文、数字、下划线，长度 2-32 位');
            return;
          }}
          if (nickname.length > 32) {{
            event.preventDefault();
            showError(registerError, '用户名长度不能超过 32 位');
            return;
          }}
          if (!EMAIL_RE.test(email)) {{
            event.preventDefault();
            showError(registerError, '请输入正确的邮箱格式');
            return;
          }}
          if (password !== confirm) {{
            event.preventDefault();
            showError(registerError, '两次输入的密码不一致');
            return;
          }}
        }});
      }}
    }})();
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


def _encode_query(value: str) -> str:
    """Minimal URL query encoding (used for bounce-back error messages)."""
    return quote(value or "", safe="")


def _try_local_login(username: str, password: str) -> Optional[Dict[str, Any]]:
    """Try local-account authentication. Returns user_info (password stripped) on success; None on failure."""
    try:
        from core.config.settings import settings as _settings

        if not _settings.auth.local_enabled:
            return None
    except Exception:
        return None

    try:
        from core.db.engine import SessionLocal
        from core.services.local_user_service import LocalUserService
    except Exception as exc:
        logger.warning("local auth unavailable: %s", exc)
        return None

    db = SessionLocal()
    try:
        service = LocalUserService(db)
        result = service.authenticate(username, password)
        if result.ok and result.user_info:
            return result.user_info
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("local auth failed unexpectedly: %s", exc)
        return None
    finally:
        db.close()


def _audit_local_login(
    request: Request, username: str, *, success: bool, user_id: Optional[str] = None
) -> None:
    """Audit local login success/failure (P0 instrumentation: closes the password-brute-force blind spot).

    Failure rows carry username + IP + UA, so the security side can cluster by IP/user/time window to detect brute forcing;
    best-effort — no exception may ever affect the login flow.
    """
    try:
        from core.db.engine import SessionLocal
        from core.db.repository import AuditLogRepository

        db = SessionLocal()
        try:
            AuditLogRepository(db).create(
                {
                    "user_id": user_id,
                    "action": "auth.local_login.success" if success else "auth.local_login.failed",
                    "resource_type": "auth",
                    "details": {"username": (username or "")[:120]},
                    "ip_address": request.client.host if request.client else None,
                    "user_agent": request.headers.get("user-agent"),
                    "status": "success" if success else "failure",
                    "error_code": None if success else 401,
                }
            )
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — an audit failure must never block login
        pass


def _login_page_path(request: Request, *, suffix: str = "") -> str:
    """The login-page GET path corresponding to the current POST (used for error bounce-back).

    - POST /login          → /login
    - POST /register       → /login
    - POST /mock-sso/login → /mock-sso/login
    - POST /mock-sso/register → /mock-sso/login
    """
    path = request.url.path or "/login"
    if path.endswith("/register"):
        path = path[: -len("/register")] + "/login"
    return path + suffix


def _parse_remember(raw: Optional[str]) -> bool:
    """Parse the login page's "keep me signed in" checkbox.

    The checkbox is checked by default: when checked, the browser submits ``remember=1``;
    when unchecked, the field is not submitted (raw is "") → False. Also accepts explicit 0/false/off/no.
    """
    return (raw or "").strip().lower() not in ("", "0", "false", "off", "no")


async def _handle_login_submit(
    request: Request,
    username: str,
    password: Optional[str],
    redirect: str,
    remember: Optional[str] = None,
) -> RedirectResponse:
    username = (username or "").strip()
    page_path = _login_page_path(request)
    if not username or not password or not password.strip():
        return RedirectResponse(
            url=f"{page_path}?redirect={_encode_query(redirect)}&error={_encode_query('请输入账号和密码')}",
            status_code=303,
        )

    remember_flag = _parse_remember(remember)

    # 1) Local accounts
    local_info = _try_local_login(username, password)
    if local_info is not None:
        _audit_local_login(request, username, success=True, user_id=local_info.get("user_id"))
        local_info["remember"] = remember_flag
        ticket = _generate_ticket(local_info)
        target = _build_redirect_target(request, redirect, ticket)
        return RedirectResponse(url=target, status_code=303)

    # 2) Known mock accounts are development-only. CE never accepts the public
    #    mock credentials, even if an old deployment accidentally left mock SSO
    #    environment variables enabled.
    if _mock_account_shortcuts_enabled():
        mock_user = _USER_BY_NAME.get(username)
        if mock_user is not None and mock_user["password"] == password:
            mock_info = _user_info_without_password(mock_user)
            mock_info["remember"] = remember_flag
            ticket = _generate_ticket(mock_info)
            target = _build_redirect_target(request, redirect, ticket)
            return RedirectResponse(url=target, status_code=303)

    _audit_local_login(request, username, success=False)
    return RedirectResponse(
        url=f"{page_path}?redirect={_encode_query(redirect)}&error={_encode_query('用户名或密码错误，请重试')}",
        status_code=303,
    )


async def _handle_register_submit(
    request: Request,
    code: str,
    username: str,
    nickname: str,
    email: str,
    password: str,
    confirm_password: str,
    real_name: str,
    phone: str,
    redirect: str,
) -> RedirectResponse:
    page_path = _login_page_path(request)

    def _bounce(msg: str) -> RedirectResponse:
        return RedirectResponse(
            url=(
                f"{page_path}?redirect={_encode_query(redirect)}"
                f"&tab=register&reg_error={_encode_query(msg)}"
            ),
            status_code=303,
        )

    try:
        from core.config.settings import settings as _settings

        if not _settings.auth.local_enabled or not is_register_allowed():
            return RedirectResponse(
                url=f"{page_path}?redirect={_encode_query(redirect)}&error={_encode_query('注册已关闭')}",
                status_code=303,
            )
    except Exception:
        pass

    if password != confirm_password:
        return _bounce("两次输入的密码不一致")

    try:
        from core.db.engine import SessionLocal
        from core.services.local_user_service import LocalUserService
    except Exception as exc:
        logger.warning("local auth module unavailable: %s", exc)
        return _bounce("注册服务暂不可用")

    db = SessionLocal()
    try:
        service = LocalUserService(db)
        result = service.register(
            code=code,
            username=username,
            nickname=nickname,
            email=email,
            password=password,
            real_name=real_name,
            phone=phone,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("register failed: %s", exc)
        db.close()
        return _bounce("注册失败，请稍后重试")
    finally:
        db.close()

    if not result.ok or not result.user_info:
        return _bounce(result.message or "注册失败")

    ticket = _generate_ticket(result.user_info)
    target = _build_redirect_target(request, redirect, ticket)
    return RedirectResponse(url=target, status_code=303)


@router.post("/login", summary="模拟 SSO 密码验证")
async def mock_login_submit(
    request: Request,
    username: str = Form(""),
    password: Optional[str] = Form(None),
    redirect: str = Form("/"),
    remember: Optional[str] = Form(None),
):
    """提交账号密码登录（表单 POST）：依次尝试本地账号与 Mock 账号验证。

    成功则生成一次性 ticket 并 303 跳回 redirect 指定页面，失败则带错误信息回跳登录页。
    ``remember`` 为登录页「记住登录状态」复选框（默认勾选 → 长会话）。
    """
    return await _handle_login_submit(request, username, password, redirect, remember)


@router.post("/register", summary="本地账号注册")
async def mock_register_submit(
    request: Request,
    code: str = Form(""),
    username: str = Form(""),
    nickname: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    real_name: str = Form(""),
    phone: str = Form(""),
    redirect: str = Form("/"),
):
    """提交本地账号注册（表单 POST）：校验注册码与各字段后创建账号并直接登录。

    成功则生成 ticket 并 303 跳回 redirect，失败带错误信息回跳注册 Tab；本地注册关闭时拒绝。
    """
    return await _handle_register_submit(
        request,
        code,
        username,
        nickname,
        email,
        password,
        confirm_password,
        real_name,
        phone,
        redirect,
    )


# ── /login alias routes (recommended for production) ───────────────────
login_router = APIRouter(tags=["Auth Login"])


@login_router.get("/login", summary="统一登录页")
async def login_page(
    request: Request,
    redirect: str = Query("/"),
    auto: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    tab: Optional[str] = Query(None),
    reg_error: Optional[str] = Query(None),
):
    """统一登录页（生产入口）：返回含登录/注册 Tab 的 HTML 页面。

    复用 Mock 登录页渲染；error / reg_error 用于回显失败提示，无需登录。
    """
    return await mock_login_page(
        request=request,
        redirect=redirect,
        auto=auto,
        error=error,
        tab=tab,
        reg_error=reg_error,
    )


@login_router.post("/login", summary="登录提交")
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: Optional[str] = Form(None),
    redirect: str = Form("/"),
    remember: Optional[str] = Form(None),
):
    """登录提交（生产入口，/login 表单 POST）：验证账号密码，成功后 303 跳回 redirect。

    与 /mock-sso/login 提交逻辑一致（先本地账号、再 Mock 账号），失败回跳登录页。
    ``remember`` 为登录页「记住登录状态」复选框（默认勾选 → 长会话）。
    """
    return await _handle_login_submit(request, username, password, redirect, remember)


@login_router.post("/register", summary="注册提交")
async def register_submit(
    request: Request,
    code: str = Form(""),
    username: str = Form(""),
    nickname: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    real_name: str = Form(""),
    phone: str = Form(""),
    redirect: str = Form("/"),
):
    """注册提交（生产入口，/register 表单 POST）：校验注册码与字段后创建本地账号并登录。

    与 /mock-sso/register 提交逻辑一致，成功 303 跳回 redirect，失败回跳注册 Tab。
    """
    return await _handle_register_submit(
        request,
        code,
        username,
        nickname,
        email,
        password,
        confirm_password,
        real_name,
        phone,
        redirect,
    )


@router.post("/ticket/exchange", summary="模拟 ticket 换取用户信息")
async def mock_ticket_exchange(body: dict):
    """Simulate the SSO ticket exchange endpoint.

    This is what the backend's ``sso_client.py`` calls when ``SSO_MOCK_ENABLED=true``.
    It can also be called externally if ``SSO_TICKET_EXCHANGE_URL`` points here.
    """
    ticket = body.get("ticket", "")
    user_info = consume_ticket(ticket)

    if user_info is None:
        return {"code": 401, "message": "Invalid or expired ticket", "data": None}

    return {
        "code": 0,
        "message": "ok",
        "data": user_info,
    }
