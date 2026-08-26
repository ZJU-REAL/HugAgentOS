function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function shell(title, body, script = "", bodyClass = "") {
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${escapeHtml(title)}</title><style>
:root{color-scheme:light;--accent:#1677ff;--accent-2:#16a6ff;--text:#172033;--secondary:#647089;
  --line:rgba(35,83,160,.13);--surface:rgba(255,255,255,.8);--page:#f4f8ff;--solid:#fff;
  --field:#fff;--soft:#e8edf5;--ok:#16a36a;--danger:#d70015;--danger-bg:#fff1f0;--glow:rgba(22,119,255,.22)}
@media(prefers-color-scheme:dark){:root{color-scheme:dark;--accent:#5b9cff;--accent-2:#42c8ff;--text:#edf4ff;
  --secondary:#9eabc0;--line:rgba(132,170,230,.18);--surface:rgba(20,29,44,.82);--page:#0b111c;
  --solid:#141d2b;--field:#151d29;--soft:#2a3444;--ok:#34d399;--danger:#ff7070;
  --danger-bg:rgba(255,107,107,.14);--glow:rgba(66,157,255,.24)}}
*{box-sizing:border-box}html,body{min-height:100%;margin:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Noto Sans CJK SC","Microsoft YaHei","Segoe UI",sans-serif;
  display:grid;place-items:center;padding:24px;background:var(--page);color:var(--text)}
.card{width:min(620px,calc(100vw - 40px));padding:34px;border:1px solid var(--line);border-radius:18px;
  background:var(--surface);box-shadow:0 18px 60px rgba(38,54,80,.1);backdrop-filter:blur(18px);
  -webkit-backdrop-filter:blur(18px);animation:materialize .42s cubic-bezier(.2,.8,.2,1) both}
h1{margin:0 0 10px;font-size:23px}.muted{color:var(--secondary);line-height:1.7}
.row{display:flex;gap:12px;flex-wrap:wrap;margin-top:22px}
button,.button{border:0;border-radius:10px;padding:10px 18px;background:var(--accent);color:#fff;font:600 14px/1.2 inherit;
  cursor:pointer;text-decoration:none;transition:transform .15s ease,filter .15s ease,opacity .15s ease}
button.secondary,.button.secondary{background:var(--soft);color:var(--text)}
button:hover,.button:hover{filter:brightness(1.05);transform:translateY(-1px)}
button:active,.button:active{transform:scale(.98)}button:disabled{cursor:default;opacity:.55;transform:none}
label{display:block;margin-top:16px;font-size:13px;color:var(--secondary)}
input,select{width:100%;margin-top:7px;padding:11px 12px;border:1px solid var(--line);border-radius:9px;
  background:var(--field);color:var(--text);font-size:14px;outline:none;transition:border-color .14s ease,box-shadow .14s ease}
input:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--glow)}
body.onboarding{display:flex;align-items:center;justify-content:center;min-height:100vh;overflow:hidden;-webkit-user-select:none;user-select:none}
body.onboarding::before,body.onboarding::after{content:"";position:fixed;pointer-events:none;border-radius:50%}
body.onboarding::before{width:620px;height:620px;left:-290px;top:-330px;background:radial-gradient(circle,var(--glow),transparent 68%)}
body.onboarding::after{width:540px;height:540px;right:-250px;bottom:-330px;
  background:radial-gradient(circle,rgba(22,166,255,.15),transparent 70%)}
body.onboarding .card{position:relative;width:min(620px,100%);padding:26px 28px 34px;border:0;border-radius:0;
  background:transparent;box-shadow:none;backdrop-filter:none;-webkit-backdrop-filter:none;text-align:center;z-index:1}
.visual{position:relative;width:176px;height:176px;margin:0 auto 18px;display:grid;place-items:center}
.halo{position:absolute;inset:24px;border-radius:50%;background:var(--glow);filter:blur(24px);animation:halo 3s ease-in-out infinite}
.orbit{position:absolute;inset:5px;border:1px solid rgba(22,119,255,.3);border-radius:50%;animation:spin 7s linear infinite}
.orbit.two{inset:22px;border-style:dashed;animation-duration:10s;animation-direction:reverse;opacity:.72}
.orbit::before,.orbit::after{content:"";position:absolute;border-radius:50%;background:var(--accent-2);
  box-shadow:0 0 0 6px rgba(22,166,255,.12),0 0 20px var(--glow)}
.orbit::before{width:10px;height:10px;left:13px;top:18px}.orbit::after{width:7px;height:7px;right:5px;bottom:37px}
.core{position:relative;width:112px;height:112px;border-radius:32px;display:grid;place-items:center;background:var(--surface);
  border:1px solid var(--line);box-shadow:0 22px 60px var(--glow),0 2px 10px rgba(0,0,0,.08);
  backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);animation:float 3.2s ease-in-out infinite}
.logo{display:block;width:92px;height:92px;border-radius:25px;object-fit:cover}
.visual.ready .orbit{border-color:rgba(22,163,106,.46)}.visual.ready .orbit::before,.visual.ready .orbit::after{background:var(--ok)}
.visual.error .orbit{animation-play-state:paused;border-color:rgba(215,0,21,.44)}
.product{margin:0 0 8px;color:var(--accent);font-size:13px;font-weight:700;letter-spacing:.09em}
body.onboarding h1{margin:0;font-size:32px;line-height:1.16;font-weight:720;letter-spacing:-.035em}
.lead{max-width:440px;margin:10px auto 0;color:var(--secondary);font-size:14px;line-height:1.65}
.wide-button{width:min(340px,100%);height:52px;margin-top:28px;border-radius:15px;
  background:linear-gradient(110deg,var(--accent),var(--accent-2));box-shadow:0 13px 30px rgba(22,119,255,.24)}
.init-error{min-height:20px;margin:12px auto -8px;color:var(--danger);font-size:12.5px}
.actions{display:none;width:min(370px,100%);margin:22px auto 0}.actions .button{width:100%;height:50px}
.progress-wrap{display:block;width:min(490px,100%);margin:23px auto 0;padding:24px 25px 22px;text-align:left;
  border:1px solid var(--line);border-radius:20px;background:var(--surface);backdrop-filter:blur(20px) saturate(145%);
  -webkit-backdrop-filter:blur(20px) saturate(145%);box-shadow:0 18px 50px rgba(18,58,118,.09);
  animation:materialize .48s cubic-bezier(.2,.8,.2,1) both}
.progress-head{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:14px}
.message{min-width:0;color:var(--secondary);font-size:13.5px;line-height:1.45;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.percent{flex:none;color:var(--accent);font-size:19px;font-weight:720;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.progress{position:relative;height:12px;border-radius:999px;background:rgba(22,119,255,.11);overflow:hidden}
.bar{position:relative;height:100%;width:0;border-radius:inherit;background:linear-gradient(90deg,var(--accent),var(--accent-2));
  box-shadow:0 0 18px var(--glow);transition:width .36s cubic-bezier(.2,.8,.2,1)}
.bar::after{content:"";position:absolute;inset:0;background:linear-gradient(110deg,transparent 28%,rgba(255,255,255,.55) 48%,transparent 68%);
  transform:translateX(-120%);animation:sweep 1.7s ease-in-out infinite}
.error{display:none;margin-top:14px;padding:11px 13px;border-radius:11px;background:var(--danger-bg);color:var(--danger);
  font-size:12.5px;line-height:1.5}.ready-text{color:var(--ok);font-weight:650}
@keyframes materialize{from{opacity:0;transform:translateY(10px) scale(.985)}to{opacity:1;transform:none}}
@keyframes spin{to{transform:rotate(360deg)}}@keyframes float{0%,100%{transform:translateY(0) scale(1)}50%{transform:translateY(-7px) scale(1.015)}}
@keyframes halo{0%,100%{opacity:.55;transform:scale(.88)}50%{opacity:1;transform:scale(1.12)}}
@keyframes sweep{55%,100%{transform:translateX(160%)}}
@media(max-width:620px){body{padding:16px}.card{width:100%;padding:26px 20px}body.onboarding .card{padding:18px 8px 24px}
  .visual{transform:scale(.88);margin-bottom:5px}body.onboarding h1{font-size:28px}.progress-wrap{padding:20px}}
@media(max-height:700px){body.onboarding{align-items:flex-start;overflow:auto}.visual{transform:scale(.82);margin-top:-14px;margin-bottom:-6px}}
@media(prefers-reduced-motion:reduce){.card,.halo,.orbit,.core,.bar::after{animation:none}button,.button,input,select,.bar{transition:none}}
@media(prefers-reduced-transparency:reduce){.card,.progress-wrap,.core{background:var(--solid);backdrop-filter:none;-webkit-backdrop-filter:none}}
</style></head><body class="${escapeHtml(bodyClass)}"><main class="card">${body}</main><script>${script}</script></body></html>`;
}

function visual(brand) {
  return `<div class="visual" id="visual" aria-hidden="true">
    <span class="halo"></span><span class="orbit"></span><span class="orbit two"></span>
    <div class="core"><img class="logo" src="/icon.png" alt="" onerror="this.style.visibility='hidden'"></div>
  </div><p class="product">${escapeHtml(brand)}</p>`;
}

export function loginPage({ brand = "HugAgentOS", waiting = false } = {}) {
  return shell(
    brand,
    `<h1>${escapeHtml(brand)}</h1><p class="muted">${waiting ? "请在系统浏览器完成登录，完成后会自动返回客户端。" : "使用系统浏览器安全登录。长期会话不会写入链接。"}</p>
    <div class="row">${waiting ? '<a class="button secondary" href="/__desktop/action?name=open-login">重新打开浏览器</a>' : '<a class="button" href="/__desktop/action?name=open-login">开始使用</a>'}</div>`,
  );
}

function fixedDualInitPage({ brand, cloudBase, localSupported }) {
  return shell(
    `初始化 · ${brand}`,
    `${visual(brand)}<h1>初始化 ${escapeHtml(brand)}</h1>
    <p class="lead">配置本机运行环境，并连接云端服务。</p>
    <input id="cloudBase" type="hidden" value="${escapeHtml(cloudBase)}">
    <div class="init-error" id="err" role="alert"></div>
    <button class="wide-button" id="go" type="button" onclick="start()">开始初始化</button>`,
    `const button=document.getElementById('go'),err=document.getElementById('err'),localSupported=${localSupported ? "true" : "false"};
if(!localSupported){button.disabled=true;err.textContent='当前安装包缺少本机运行资源，请重新下载安装包。'}
function start(){if(!localSupported)return;button.disabled=true;button.textContent='正在启动…';const base=document.getElementById('cloudBase').value;
location.href='/__desktop/action?name=provision&mode=dual&base='+encodeURIComponent(base)}`,
    "onboarding",
  );
}

export function initPage({
  brand = "HugAgentOS",
  cloudBase = "",
  fixedDual = false,
  mode = "local_only",
  localSupported = true,
} = {}) {
  if (fixedDual) return fixedDualInitPage({ brand, cloudBase, localSupported });
  return shell(
    "选择运行模式",
    `<h1>选择运行模式</h1><p class="muted">UOS 完整包可离线托管本机服务；云端和双模式需要团队服务器地址。</p>
    <form id="f"><label>运行模式<select id="mode">
      <option value="local_only" ${mode === "local_only" ? "selected" : ""} ${localSupported ? "" : "disabled"}>本机模式</option>
      <option value="cloud_only" ${mode === "cloud_only" ? "selected" : ""}>云端模式</option>
      <option value="dual" ${mode === "dual" ? "selected" : ""} ${localSupported ? "" : "disabled"}>双模式（云端为主 + 本机执行）</option>
    </select></label><label id="cloud">服务器地址<input id="base" value="${escapeHtml(cloudBase)}" placeholder="https://agent.example.gov.cn"></label>
    <div class="row"><button type="submit">保存并重启</button></div></form>`,
    `const mode=document.getElementById('mode'),cloud=document.getElementById('cloud');
function sync(){cloud.hidden=mode.value==='local_only'}mode.onchange=sync;sync();
document.getElementById('f').onsubmit=event=>{event.preventDefault();location.href='/__desktop/action?name=provision&mode='+
encodeURIComponent(mode.value)+'&base='+encodeURIComponent(document.getElementById('base').value)}`,
  );
}

export function setupPage({ brand = "HugAgentOS", dual = false, localSupported = true } = {}) {
  return shell(
    `初始化 · ${brand}`,
    `${visual(brand)}<h1 id="title">正在初始化</h1>
    <p class="lead">${dual ? "正在配置本机运行环境并连接云端服务。" : "正在配置本机运行环境。"}</p>
    <section class="actions" id="actions" aria-label="初始化操作">
      <button class="button" id="install" type="button">重新尝试</button>
    </section>
    <section class="progress-wrap" aria-live="polite">
      <div class="progress-head"><span class="message" id="message">正在准备…</span><span class="percent" id="percent">0%</span></div>
      <div class="progress" role="progressbar" aria-label="安装进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
        <div class="bar" id="bar"></div>
      </div>
      <div class="error" id="error" role="alert"></div>
    </section>`,
    `const message=document.getElementById('message'),bar=document.getElementById('bar'),percent=document.getElementById('percent');
const progress=document.querySelector('.progress'),button=document.getElementById('install'),actions=document.getElementById('actions');
let installing=false,autoStartAttempted=false,pollTimer=null;
function schedule(delay=850){if(pollTimer)clearTimeout(pollTimer);pollTimer=setTimeout(poll,delay)}
function setProgress(value){const next=Math.max(0,Math.min(100,Number(value)||0));bar.style.width=next+'%';percent.textContent=next+'%';progress.setAttribute('aria-valuenow',String(next))}
function showError(text){installing=false;document.getElementById('error').textContent=text;document.getElementById('error').style.display='block';
document.getElementById('visual').classList.add('error');actions.style.display='block';button.disabled=false}
async function installLocal(){if(!${localSupported ? "true" : "false"}){showError('当前安装包缺少本机运行资源，请重新下载安装包。');return}
installing=true;actions.style.display='none';document.getElementById('error').style.display='none';document.getElementById('visual').className='visual';
message.textContent='正在准备本机服务…';try{const response=await fetch('/__desktop/setup/install',{method:'POST'});
if(!response.ok)throw new Error('HTTP '+response.status);await response.json();schedule(100)}catch(error){showError('无法启动安装：'+error.message)}}
async function poll(){try{const response=await fetch('/__desktop/setup/status',{cache:'no-store'});if(!response.ok)throw new Error('HTTP '+response.status);
const status=await response.json();setProgress(status.progress);message.textContent=status.message||'正在准备…';
if(!status.supported){showError('当前安装包缺少本机运行资源，请重新下载安装包。');return}
if(status.phase==='error'){showError(status.message||'安装失败，请重试。');return}
if(status.ready){setProgress(100);document.getElementById('visual').classList.add('ready');message.innerHTML='<span class="ready-text">本机服务已就绪，正在进入…</span>';
setTimeout(()=>location.replace(status.continue_url||'/'),650);return}
if(status.phase==='installing'||status.phase==='starting'){installing=true;actions.style.display='none'}
else if(!installing&&!autoStartAttempted){autoStartAttempted=true;await installLocal();return}
}catch(error){showError('读取安装状态失败：'+error.message);schedule(1400);return}schedule()}
button.onclick=installLocal;poll()`,
    "onboarding",
  );
}

export function serverConfigPage(base = "") {
  return shell(
    "设置服务器地址",
    `<h1>设置服务器地址</h1><p class="muted">保存后客户端会重启并连接新的团队服务器。</p>
    <form id="f"><label>服务器地址<input id="base" value="${escapeHtml(base)}" required></label>
    <div class="row"><button>保存并重启</button><a class="button secondary" href="/">取消</a></div></form>`,
    `document.getElementById('f').onsubmit=event=>{event.preventDefault();location.href='/__desktop/action?name=save-server&base='+
encodeURIComponent(document.getElementById('base').value)}`,
  );
}

export function errorPage(message) {
  return shell("HugAgentOS", `<h1>启动失败</h1><p class="muted">${escapeHtml(message)}</p>
  <div class="row"><a class="button" href="/__desktop/action?name=reload">重试</a></div>`);
}

export { escapeHtml };
