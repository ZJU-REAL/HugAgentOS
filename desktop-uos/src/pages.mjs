function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function shell(title, body, script = "") {
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${escapeHtml(title)}</title><style>
:root{color-scheme:light dark;font-family:"Noto Sans CJK SC","Microsoft YaHei",system-ui,sans-serif}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f6fa;color:#20242c}
.card{width:min(620px,calc(100vw - 40px));padding:34px;border:1px solid #dfe4ed;border-radius:16px;background:#fff;box-shadow:0 18px 60px #26365018}
h1{margin:0 0 10px;font-size:23px}.muted{color:#697386;line-height:1.7}.row{display:flex;gap:12px;flex-wrap:wrap;margin-top:22px}
button,.button{border:0;border-radius:9px;padding:10px 18px;background:#2563eb;color:#fff;font-size:14px;cursor:pointer;text-decoration:none}
button.secondary,.button.secondary{background:#e8edf5;color:#283449}label{display:block;margin-top:16px;font-size:13px;color:#566176}
input,select{width:100%;margin-top:7px;padding:11px 12px;border:1px solid #ccd3df;border-radius:8px;background:transparent;color:inherit;font-size:14px}
.progress{height:9px;background:#e6eaf1;border-radius:9px;overflow:hidden;margin-top:22px}.bar{height:100%;width:0;background:#2563eb;transition:width .2s}
pre{max-height:170px;overflow:auto;padding:12px;border-radius:8px;background:#111827;color:#d1d5db;font-size:11px;white-space:pre-wrap}
@media(prefers-color-scheme:dark){body{background:#17191d;color:#e8eaed}.card{background:#22252b;border-color:#363b45}.muted,label{color:#aeb5c2}.secondary{background:#363b45!important;color:#e8eaed!important}}
</style></head><body><main class="card">${body}</main><script>${script}</script></body></html>`;
}

export function loginPage({ brand = "HugAgentOS", waiting = false } = {}) {
  return shell(
    brand,
    `<h1>${escapeHtml(brand)}</h1><p class="muted">${waiting ? "请在系统浏览器完成登录，完成后会自动返回客户端。" : "使用系统浏览器安全登录。长期会话不会写入链接。"}</p>
    <div class="row">${waiting ? '<a class="button secondary" href="/__desktop/action?name=open-login">重新打开浏览器</a>' : '<a class="button" href="/__desktop/action?name=open-login">开始使用</a>'}</div>`,
  );
}

export function initPage({ cloudBase = "", mode = "local_only", localSupported = true } = {}) {
  return shell(
    "选择运行模式",
    `<h1>选择运行模式</h1><p class="muted">UOS 完整包可离线托管本机服务；云端和双模式需要团队服务器地址。</p>
    <form id="f"><label>运行模式<select id="mode">
      <option value="local_only" ${mode === "local_only" ? "selected" : ""} ${localSupported ? "" : "disabled"}>本机模式</option>
      <option value="cloud_only" ${mode === "cloud_only" ? "selected" : ""}>云端模式</option>
      <option value="dual" ${mode === "dual" ? "selected" : ""} ${localSupported ? "" : "disabled"}>双模式（云端为主 + 本机执行）</option>
    </select></label><label id="cloud">服务器地址<input id="base" value="${escapeHtml(cloudBase)}" placeholder="https://agent.example.gov.cn"></label>
    <div class="row"><button type="submit">保存并重启</button></div></form>`,
    `const m=document.getElementById('mode'),c=document.getElementById('cloud');function sync(){c.hidden=m.value==='local_only'}m.onchange=sync;sync();document.getElementById('f').onsubmit=e=>{e.preventDefault();location.href='/__desktop/action?name=provision&mode='+encodeURIComponent(m.value)+'&base='+encodeURIComponent(document.getElementById('base').value)}`,
  );
}

export function setupPage({ localSupported = true } = {}) {
  return shell(
    "本机服务",
    `<h1>本机服务</h1><p class="muted" id="message">正在检查离线运行环境…</p><div class="progress"><div class="bar" id="bar"></div></div>
    <pre id="logs" hidden></pre><div class="row"><button id="install" ${localSupported ? "" : "disabled"}>一键安装并启动</button><a class="button secondary" href="/__desktop/action?name=server-config">连接云端服务器</a></div>`,
    `const msg=document.getElementById('message'),bar=document.getElementById('bar'),logs=document.getElementById('logs'),btn=document.getElementById('install');
async function poll(){try{const r=await fetch('/__desktop/setup/status'),s=await r.json();msg.textContent=s.message;bar.style.width=s.progress+'%';logs.textContent=(s.logs||[]).join('\n');logs.hidden=!logs.textContent;btn.disabled=s.phase==='installing'||s.phase==='starting';if(s.ready){location.replace(s.continue_url||'/');return}}catch(e){msg.textContent='状态读取失败：'+e}setTimeout(poll,1000)}
btn.onclick=async()=>{btn.disabled=true;await fetch('/__desktop/setup/install',{method:'POST'});poll()};poll();`,
  );
}

export function serverConfigPage(base = "") {
  return shell(
    "设置服务器地址",
    `<h1>设置服务器地址</h1><p class="muted">保存后客户端会重启并连接新的团队服务器。</p><form id="f"><label>服务器地址<input id="base" value="${escapeHtml(base)}" required></label><div class="row"><button>保存并重启</button><a class="button secondary" href="/">取消</a></div></form>`,
    `document.getElementById('f').onsubmit=e=>{e.preventDefault();location.href='/__desktop/action?name=save-server&base='+encodeURIComponent(document.getElementById('base').value)}`,
  );
}

export function errorPage(message) {
  return shell("HugAgentOS", `<h1>启动失败</h1><p class="muted">${escapeHtml(message)}</p><div class="row"><a class="button" href="/__desktop/action?name=reload">重试</a></div>`);
}

export { escapeHtml };
