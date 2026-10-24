"""统一的站点密码验证页 —— 所有加密站点共用这一个页面。

页面由后端直出，自带样式与脚本、不依赖前端构建产物，因此在任何部署形态下外观一致。
校验通过后 ``__api/access`` 下发访问凭据 cookie，页面自行刷新回到站点内容。
"""

from __future__ import annotations

import html
import json

_TEXT = {
    "zh": {
        "heading": "需要访问密码",
        "hint": "请向站点创建者索取密码",
        "placeholder": "访问密码",
        "submit": "进入站点",
        "wrong": "密码不正确",
        "failed": "验证失败，请稍后再试",
        "too_many": "尝试过于频繁，请稍后再试",
    },
    "en": {
        "heading": "Password required",
        "hint": "Ask the site owner for the password",
        "placeholder": "Access password",
        "submit": "Enter site",
        "wrong": "Incorrect password",
        "failed": "Verification failed, please try again later",
        "too_many": "Too many attempts, please try again later",
    },
}

# 脚本运行时才用得到的文案。
_SCRIPT_KEYS = ("wrong", "failed", "too_many")

_PAGE = """<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{title}</title>
<style>
:root{{color-scheme:light dark;--bg:#F5F7FB;--card:#FFFFFF;--text:#1F2937;--muted:#8A94A6;
--border:#E3E8EF;--primary:#126DFF;--primary-hover:#3C87FF;--danger:#D92D20}}
@media (prefers-color-scheme:dark){{:root{{--bg:#12171F;--card:#161C25;--text:#E6EAF2;
--muted:#8A94A6;--border:#2B3442;--primary:#3E8BFF;--primary-hover:#5FA0FF;--danger:#F97066}}}}
*{{box-sizing:border-box}}
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:var(--bg);color:var(--text);padding:24px;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
.card{{width:100%;max-width:360px;background:var(--card);border:1px solid var(--border);
border-radius:16px;padding:32px 28px;text-align:center}}
.lock{{width:40px;height:40px;margin:0 auto 16px;color:var(--muted)}}
h1{{margin:0 0 8px;font-size:17px;font-weight:600}}
p{{margin:0 0 20px;font-size:13px;color:var(--muted)}}
.name{{margin:0 0 6px;font-size:13px;color:var(--muted);word-break:break-all}}
input{{width:100%;height:40px;padding:0 12px;font-size:14px;color:var(--text);
background:transparent;border:1px solid var(--border);border-radius:10px;outline:none}}
input:focus{{border-color:var(--primary)}}
button{{width:100%;height:40px;margin-top:12px;font-size:14px;font-weight:500;color:#fff;
background:var(--primary);border:0;border-radius:10px;cursor:pointer}}
button:hover{{background:var(--primary-hover)}}
button:disabled{{opacity:.6;cursor:default}}
.err{{min-height:18px;margin-top:10px;font-size:12px;color:var(--danger)}}
</style>
</head>
<body>
<main class="card">
<svg class="lock" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"
 stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
<rect x="4" y="10" width="16" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>
</svg>
<h1>{heading}</h1>
<div class="name">{title}</div>
<p>{hint}</p>
<form id="f">
<input id="p" type="password" autocomplete="current-password" placeholder="{placeholder}" autofocus>
<button id="b" type="submit">{submit}</button>
<div class="err" id="e" role="alert"></div>
</form>
</main>
<script>
(function(){{
  var T = {texts};
  var f = document.getElementById('f'), p = document.getElementById('p');
  var b = document.getElementById('b'), e = document.getElementById('e');
  f.addEventListener('submit', function(ev){{
    ev.preventDefault();
    if (!p.value) return;
    b.disabled = true; e.textContent = '';
    fetch('{unlock_url}', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{password: p.value}}), credentials: 'same-origin'
    }}).then(function(r){{
      if (r.ok) {{ location.reload(); return; }}
      e.textContent = r.status === 429 ? T.too_many : T.wrong;
      b.disabled = false; p.select();
    }}).catch(function(){{
      e.textContent = T.failed; b.disabled = false;
    }});
  }});
}})();
</script>
</body>
</html>
"""


def render_site_gate(base_path: str, site_title: str, accept_language: str) -> str:
    lang = "zh" if "zh" in accept_language.lower() else "en"
    texts = _TEXT[lang]
    return _PAGE.format(
        lang="zh-CN" if lang == "zh" else "en",
        # 解锁接口写成绝对路径：验证页可能落在 /site/<slug>/sub/page.html 这类深层地址上，
        # 相对路径会解析到子目录下的不存在接口。
        unlock_url=f"{base_path}__api/access",
        title=html.escape(site_title or ""),
        heading=texts["heading"],
        hint=texts["hint"],
        placeholder=texts["placeholder"],
        submit=texts["submit"],
        # 标题类文案已直接渲染进 HTML，脚本只用得上这三条报错。
        texts=json.dumps({k: texts[k] for k in _SCRIPT_KEYS}, ensure_ascii=False),
    )
