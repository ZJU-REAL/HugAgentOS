//! Platform window chrome, shared frontend surfaces, menus and safe areas.
use super::brand;

// ── 一体化桌面标题栏 ───────────────────────────────────────────────────────
//
// 主窗口关闭系统 decorations，避免「系统标题栏 + 原生菜单栏」占两行。Windows/Linux
// 保留一行紧凑菜单和窗口控制，整条背景延续最左侧模块导航底色，不重复品牌 Logo。
// 菜单靠左排列，侧边栏自己的品牌区从标题栏下方开始。
// 中间空白仍承担窗口拖动。壳动作走导航哨兵，由
// lib.rs 拦截执行，不依赖远程源下不稳定的 Tauri IPC。

const TITLEBAR_HEIGHT: u8 = 34;
const TB_OFFSET_SPA: &str =
    ":root{--hugagent-desktop-titlebar-height:34px;--hugagent-desktop-sidebar-width:344px}body{box-sizing:border-box!important;padding-top:0!important}.ant-message{top:calc(var(--hugagent-desktop-titlebar-height) + 8px)!important}.ant-notification-top,.ant-notification-topLeft,.ant-notification-topRight{top:calc(var(--hugagent-desktop-titlebar-height) + 24px)!important}";
const TB_OFFSET_PAGE: &str =
    ":root{--hugagent-desktop-titlebar-height:34px;--hugagent-desktop-sidebar-width:280px}body{box-sizing:border-box!important;padding-top:34px!important}.ant-message{top:calc(var(--hugagent-desktop-titlebar-height) + 8px)!important}.ant-notification-top,.ant-notification-topLeft,.ant-notification-topRight{top:calc(var(--hugagent-desktop-titlebar-height) + 24px)!important}";

// Tao's traffic-light y inset controls the native titlebar container height,
// not the button's top edge. The 21px inset keeps the controls inside this
// 28px drag region. The workspace begins below that region.
const MAC_TITLEBAR_HEIGHT: u8 = 28;
// Reserve one shared top region so native controls and module navigation never overlap.
const MAC_OFFSET_SPA: &str =
    ":root{--hugagent-desktop-titlebar-height:28px}body{box-sizing:border-box!important;padding-top:0!important}.ant-message{top:calc(var(--hugagent-desktop-titlebar-height) + 8px)!important}.ant-notification-top,.ant-notification-topLeft,.ant-notification-topRight{top:calc(var(--hugagent-desktop-titlebar-height) + 24px)!important}";
const MAC_OFFSET_PAGE: &str =
    ":root{--hugagent-desktop-titlebar-height:28px}body{box-sizing:border-box!important;padding-top:28px!important}.ant-message{top:calc(var(--hugagent-desktop-titlebar-height) + 8px)!important}.ant-notification-top,.ant-notification-topLeft,.ant-notification-topRight{top:calc(var(--hugagent-desktop-titlebar-height) + 24px)!important}";

// 这条标题栏是**注进 SPA 自己那份文档**的（见 inject_after_body），所以 `<html>` 上的
// data-theme 对它同样生效，直接引用应用令牌即可两档自动跟随 —— 不需要再写一套深色覆盖，
// 也不需要 prefers-color-scheme（那会和手动 light/dark/system 三档打架）。
const TB_CSS: &str = r##"
#hugagent-titlebar{position:fixed;inset:0 0 auto 0;height:34px;z-index:2147483647;display:flex;align-items:stretch;background-color:var(--module-rail-bg,var(--color-bg-layout));background-image:var(--module-rail-gradient,none);background-size:100vw 100vh;background-attachment:fixed;border:0;box-shadow:none;font-family:var(--font-family,-apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI","Microsoft YaHei UI","Microsoft YaHei",sans-serif);color:var(--color-text);-webkit-user-select:none;user-select:none}
#hugagent-titlebar *{box-sizing:border-box}
#hugagent-titlebar .tb-sidebarZone{flex:0 0 var(--hugagent-desktop-sidebar-width);min-width:max-content;height:100%;padding:0 6px;display:flex;align-items:center;gap:2px;background:transparent;transition:flex-basis .16s ease;overflow:visible}
#hugagent-titlebar .tb-mainChrome{flex:1;min-width:0;height:100%;display:flex;align-items:center;background:transparent;border:0}
#hugagent-titlebar .tb-spacer{flex:1;height:100%;min-width:48px}
#hugagent-titlebar .tb-menu{display:flex;align-items:stretch;height:100%;flex:0 0 auto}
#hugagent-titlebar .tb-menuGroup{position:relative;height:100%;display:flex;align-items:stretch}
#hugagent-titlebar .tb-menuLabel{height:26px;margin:4px 0;padding:0 7px;border:0;border-radius:6px;background:transparent;color:var(--color-text-secondary);display:flex;align-items:center;justify-content:center;font-family:inherit;font-size:12px;line-height:1;cursor:default}
#hugagent-titlebar .tb-menuLabel:hover,#hugagent-titlebar .tb-menuGroup.open>.tb-menuLabel{background:var(--color-fill-hover)}
#hugagent-titlebar .tb-menuLabel:focus-visible,#hugagent-titlebar .tb-windowButton:focus-visible{outline:2px solid var(--color-primary);outline-offset:-3px}
#hugagent-titlebar .tb-drop{display:none;position:absolute;top:32px;left:0;min-width:218px;padding:6px;background:var(--color-bg-elevated);border:1px solid var(--color-border);border-radius:8px;box-shadow:0 10px 28px color-mix(in srgb, var(--color-text) 16%, transparent)}
#hugagent-titlebar .tb-menuGroup.open>.tb-drop{display:block}
#hugagent-titlebar .tb-item{display:flex;align-items:center;justify-content:space-between;gap:18px;width:100%;min-height:34px;padding:7px 11px;border:0;border-radius:6px;background:transparent;color:var(--color-text);font:13px/1.3 inherit;text-align:left;white-space:nowrap;cursor:default}
#hugagent-titlebar .tb-item:hover,#hugagent-titlebar .tb-item:focus-visible{background:var(--color-primary-light);color:var(--color-primary);outline:none}
#hugagent-titlebar .tb-shortcut{color:var(--color-text-tertiary);font-size:12px}
#hugagent-titlebar .tb-sep{height:1px;margin:5px 6px;background:var(--color-border)}
#hugagent-titlebar .tb-controls{display:flex;align-items:stretch;height:100%;margin-left:0}
#hugagent-titlebar .tb-windowButton{width:46px;height:100%;padding:0;border:0;background:transparent;color:var(--color-text-secondary);display:flex;align-items:center;justify-content:center;cursor:default}
#hugagent-titlebar .tb-windowButton:hover{background:var(--color-fill-hover)}
/* dark-ok: #E81123 是 Windows 关闭键的平台约定红，两档都得是这个红，不跟主题翻转 */
#hugagent-titlebar .tb-windowButton.close:hover{background:#E81123;color:#fff}
"##;

const TB_MENU: &str = r##"<nav class="tb-menu" aria-label="应用菜单" data-i18n-aria="app_menu">
<div class="tb-menuGroup" data-menu="file"><button class="tb-menuLabel" type="button" aria-haspopup="menu" aria-expanded="false" aria-controls="hugagent-file-menu" data-i18n="file">文件</button><div class="tb-drop" id="hugagent-file-menu" role="menu" aria-label="文件" data-i18n-aria="file">
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-act="new_window"><span data-i18n="new_window">新建窗口</span><span class="tb-shortcut" aria-hidden="true">Ctrl+Shift+N</span></button>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-act="new_chat"><span data-i18n="new_chat">新建对话</span><span class="tb-shortcut" aria-hidden="true">Ctrl+N</span></button>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-act="open_folder"><span data-i18n="open_folder">打开文件夹…</span></button>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-act="run_mode"><span data-i18n="run_mode">运行模式…</span></button>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-act="server_config"><span data-i18n="server_config">设置服务器地址…</span></button>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-act="local_server"><span data-i18n="local_server">本机服务…</span></button>
  <div class="tb-sep" role="separator"></div>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-win="quit"><span data-i18n="quit">退出</span></button>
</div></div>
<div class="tb-menuGroup" data-menu="edit"><button class="tb-menuLabel" type="button" aria-haspopup="menu" aria-expanded="false" aria-controls="hugagent-edit-menu" data-i18n="edit">编辑</button><div class="tb-drop" id="hugagent-edit-menu" role="menu" aria-label="编辑" data-i18n-aria="edit">
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-edit="undo"><span data-i18n="undo">撤销</span><span class="tb-shortcut" aria-hidden="true">Ctrl+Z</span></button>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-edit="redo"><span data-i18n="redo">重做</span><span class="tb-shortcut" aria-hidden="true">Ctrl+Y</span></button>
  <div class="tb-sep" role="separator"></div>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-edit="cut"><span data-i18n="cut">剪切</span><span class="tb-shortcut" aria-hidden="true">Ctrl+X</span></button>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-edit="copy"><span data-i18n="copy">复制</span><span class="tb-shortcut" aria-hidden="true">Ctrl+C</span></button>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-edit="paste"><span data-i18n="paste">粘贴</span><span class="tb-shortcut" aria-hidden="true">Ctrl+V</span></button>
  <div class="tb-sep" role="separator"></div>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-edit="selectAll"><span data-i18n="select_all">全选</span><span class="tb-shortcut" aria-hidden="true">Ctrl+A</span></button>
</div></div>
<div class="tb-menuGroup" data-menu="view"><button class="tb-menuLabel" type="button" aria-haspopup="menu" aria-expanded="false" aria-controls="hugagent-view-menu" data-i18n="view">视图</button><div class="tb-drop" id="hugagent-view-menu" role="menu" aria-label="视图" data-i18n-aria="view">
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-act="reload"><span data-i18n="reload">重新加载</span><span class="tb-shortcut" aria-hidden="true">Ctrl+R</span></button>
  <div class="tb-sep" role="separator"></div>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-act="zoom_in"><span data-i18n="zoom_in">放大</span><span class="tb-shortcut" aria-hidden="true">Ctrl++</span></button>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-act="zoom_out"><span data-i18n="zoom_out">缩小</span><span class="tb-shortcut" aria-hidden="true">Ctrl+-</span></button>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-act="zoom_reset"><span data-i18n="zoom_reset">实际大小</span><span class="tb-shortcut" aria-hidden="true">Ctrl+0</span></button>
  <div class="tb-sep" role="separator"></div>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-win="fullscreen"><span data-i18n="fullscreen">全屏</span><span class="tb-shortcut" aria-hidden="true">F11</span></button>
</div></div>
<div class="tb-menuGroup" data-menu="help"><button class="tb-menuLabel" type="button" aria-haspopup="menu" aria-expanded="false" aria-controls="hugagent-help-menu" data-i18n="help">帮助</button><div class="tb-drop" id="hugagent-help-menu" role="menu" aria-label="帮助" data-i18n-aria="help">
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-act="check_update"><span data-i18n="check_update">检查更新…</span></button>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-act="website"><span data-i18n="website">访问官网</span></button>
  <div class="tb-sep" role="separator"></div>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-act="about"><span data-i18n="about">关于</span></button>
</div></div>
</nav>"##;

const TB_CONTROLS: &str = r##"<div class="tb-controls">
<button class="tb-windowButton" type="button" data-win="minimize" aria-label="最小化" title="最小化" data-i18n-aria="minimize"><svg width="11" height="11" viewBox="0 0 12 12"><path d="M2.5 6.5h7" fill="none" stroke="currentColor" stroke-width="1.1"/></svg></button>
<button class="tb-windowButton" type="button" data-win="toggle-maximize" aria-label="最大化或还原" title="最大化 / 还原" data-i18n-aria="maximize_restore"><svg width="10" height="10" viewBox="0 0 12 12"><rect x="2.5" y="2.5" width="7" height="7" fill="none" stroke="currentColor" stroke-width="1.1"/></svg></button>
<button class="tb-windowButton close" type="button" data-win="close" aria-label="关闭" title="关闭" data-i18n-aria="close"><svg width="11" height="11" viewBox="0 0 12 12"><path d="m3 3 6 6m0-6L3 9" fill="none" stroke="currentColor" stroke-width="1.2"/></svg></button>
</div>"##;

const TB_JS: &str = r##"(function(){
var bar=document.getElementById('hugagent-titlebar');if(!bar)return;
document.documentElement.dataset.desktopPlatform='windows';
// 快速问答使用独立原生小窗，不展示主窗口标题栏。
if(new URLSearchParams(location.search).get('quickask')==='1'){
  bar.remove();var style=document.getElementById('hugagent-titlebar-style');if(style)style.remove();return;
}
var desktopCopy={
  'zh-CN':{
    chrome:'桌面菜单栏',app_menu:'应用菜单',
    file:'文件',edit:'编辑',view:'视图',help:'帮助',new_chat:'新建对话',new_window:'新建窗口',run_mode:'运行模式…',open_folder:'打开文件夹…',
    server_config:'设置服务器地址…',local_server:'本机服务…',quit:'退出',undo:'撤销',redo:'重做',
    cut:'剪切',copy:'复制',paste:'粘贴',select_all:'全选',reload:'重新加载',fullscreen:'全屏',
    zoom_in:'放大',zoom_out:'缩小',zoom_reset:'实际大小',
    check_update:'检查更新…',website:'访问官网',about:'关于',minimize:'最小化',
    maximize_restore:'最大化 / 还原',close:'关闭'
  },
  en:{
    chrome:'Desktop menu bar',app_menu:'Application menu',
    file:'File',edit:'Edit',view:'View',help:'Help',new_chat:'New Chat',new_window:'New Window',run_mode:'Run Mode…',open_folder:'Open Folder…',
    server_config:'Server Address…',local_server:'Local Service…',quit:'Exit',undo:'Undo',redo:'Redo',
    cut:'Cut',copy:'Copy',paste:'Paste',select_all:'Select All',reload:'Reload',fullscreen:'Full Screen',
    zoom_in:'Zoom In',zoom_out:'Zoom Out',zoom_reset:'Actual Size',
    check_update:'Check for Updates…',website:'Visit Website',about:'About',minimize:'Minimize',
    maximize_restore:'Maximize / Restore',close:'Close'
  }
};
function desktopLang(){
  try{var saved=localStorage.getItem('jx_lang');if(saved==='en'||saved==='zh-CN')return saved;}catch(error){}
  var htmlLang=String(document.documentElement.lang||'').toLowerCase();
  if(htmlLang.indexOf('en')===0)return 'en';if(htmlLang.indexOf('zh')===0)return 'zh-CN';
  return String(navigator.language||'').toLowerCase().indexOf('en')===0?'en':'zh-CN';
}
function syncLocale(){
  var copy=desktopCopy[desktopLang()];if(bar.getAttribute('aria-label')!==copy.chrome)bar.setAttribute('aria-label',copy.chrome);
  bar.querySelectorAll('[data-i18n]').forEach(function(node){var value=copy[node.dataset.i18n];if(value&&node.textContent!==value)node.textContent=value;});
  bar.querySelectorAll('[data-i18n-aria]').forEach(function(node){
    var value=copy[node.dataset.i18nAria];if(!value)return;
    if(node.getAttribute('aria-label')!==value)node.setAttribute('aria-label',value);
    if(node.tagName==='BUTTON'&&node.title!==value)node.title=value;
  });
}
var mode=window.__HG_DESKTOP__&&window.__HG_DESKTOP__.provision_mode;
if(mode==='dual'){
  ['run_mode','server_config','local_server'].forEach(function(action){
    var item=bar.querySelector('[data-act="'+action+'"]');if(item)item.remove();
  });
}
if(mode==='cloud_only'){
  var folder=bar.querySelector('[data-act="open_folder"]');if(folder)folder.remove();
}
var groups=Array.prototype.slice.call(bar.querySelectorAll('.tb-menuGroup'));
var menuItems=Array.prototype.slice.call(bar.querySelectorAll('.tb-item'));
function itemsFor(group){return Array.prototype.slice.call(group.querySelectorAll('.tb-item'));}
function closeMenus(restoreLabel){
  groups.forEach(function(group){
    group.classList.remove('open');
    var label=group.querySelector('.tb-menuLabel');if(label)label.setAttribute('aria-expanded','false');
  });
  menuItems.forEach(function(item){item.tabIndex=-1;});
  if(restoreLabel)restoreLabel.focus();
}
function openMenu(group,focusIndex){
  closeMenus(false);group.classList.add('open');
  var label=group.querySelector('.tb-menuLabel');if(label)label.setAttribute('aria-expanded','true');
  var items=itemsFor(group);
  if(items.length&&focusIndex!=null){
    var index=Math.max(0,Math.min(items.length-1,focusIndex));items[index].tabIndex=0;items[index].focus();
  }
}
function adjacentGroup(group,delta){
  var index=groups.indexOf(group);return groups[(index+delta+groups.length)%groups.length];
}
function sentinel(path){window.location.href=path;}
// 缩放不能走哨兵：那次「发起即取消」的导航会让 WebView2 把 ZoomFactor 重置回 1.0，刚设上的
// 档位当场失效。改用 fetch 投递给本地反代，不碰导航。动作 id 与原生菜单同名。
function shellZoom(action){fetch('/__desktop/zoom/'+action,{method:'POST'});}
var ZOOM_KEYS={'=':'zoom_in','+':'zoom_in','add':'zoom_in','-':'zoom_out','_':'zoom_out','subtract':'zoom_out','0':'zoom_reset'};
var lastEditTarget=null;
document.addEventListener('focusin',function(event){if(!bar.contains(event.target))lastEditTarget=event.target;});
document.addEventListener('keydown',function(event){
  var key=String(event.key||'').toLowerCase();
  if(event.ctrlKey&&!event.altKey&&key==='n'){
    event.preventDefault();sentinel('/__desktop/menu?action='+(event.shiftKey?'new_window':'new_chat'));
  }else if(event.ctrlKey&&!event.altKey&&key==='r'){
    event.preventDefault();sentinel('/__desktop/menu?action=reload');
  }else if(event.ctrlKey&&!event.altKey&&ZOOM_KEYS[key]){
    event.preventDefault();shellZoom(ZOOM_KEYS[key]);
  }else if(event.key==='F11'){
    event.preventDefault();sentinel('/__desktop/win?action=fullscreen');
  }
});
// Ctrl+滚轮同样交给壳层。保持 passive：WebView 的原生缩放热键本就是关的（Tauri
// zoom_hotkeys_enabled 默认 false），没有默认行为要拦；而 non-passive 的 wheel 监听会让
// 整个文档退出合成器线程滚动，把长对话列表的滚动一起拖慢。
var wheelZoomAt=0;
window.addEventListener('wheel',function(event){
  if(!event.ctrlKey)return;
  var now=Date.now();if(now-wheelZoomAt<120)return;wheelZoomAt=now;
  shellZoom(event.deltaY<0?'zoom_in':'zoom_out');
},{passive:true});
groups.forEach(function(group){
  var label=group.querySelector('.tb-menuLabel');var drop=group.querySelector('.tb-drop');
  label.addEventListener('click',function(event){
    event.stopPropagation();if(group.classList.contains('open'))closeMenus(label);else openMenu(group,0);
  });
  label.addEventListener('keydown',function(event){
    if(event.key==='ArrowDown'||event.key==='Enter'||event.key===' '){event.preventDefault();openMenu(group,0);}
    else if(event.key==='ArrowUp'){event.preventDefault();openMenu(group,itemsFor(group).length-1);}
    else if(event.key==='ArrowLeft'||event.key==='ArrowRight'){
      event.preventDefault();adjacentGroup(group,event.key==='ArrowLeft'?-1:1).querySelector('.tb-menuLabel').focus();
    }else if(event.key==='Escape'){event.preventDefault();closeMenus(label);}
  });
  drop.addEventListener('keydown',function(event){
    var items=itemsFor(group);var current=items.indexOf(document.activeElement);var next=current;
    if(event.key==='ArrowDown')next=(current+1+items.length)%items.length;
    else if(event.key==='ArrowUp')next=(current-1+items.length)%items.length;
    else if(event.key==='Home')next=0;
    else if(event.key==='End')next=items.length-1;
    else if(event.key==='ArrowLeft'||event.key==='ArrowRight'){
      event.preventDefault();openMenu(adjacentGroup(group,event.key==='ArrowLeft'?-1:1),0);return;
    }else if(event.key==='Escape'){event.preventDefault();closeMenus(label);return;}
    else if(event.key==='Tab'){closeMenus(false);return;}
    else return;
    event.preventDefault();items.forEach(function(item){item.tabIndex=-1;});items[next].tabIndex=0;items[next].focus();
  });
});
bar.querySelectorAll('[data-win]').forEach(function(item){item.addEventListener('click',function(event){
  event.stopPropagation();closeMenus(false);sentinel('/__desktop/win?action='+encodeURIComponent(item.dataset.win));
});});
bar.querySelectorAll('[data-act]').forEach(function(item){item.addEventListener('click',function(event){
  event.stopPropagation();closeMenus(false);
  var action=item.dataset.act;
  if(action.indexOf('zoom_')===0)shellZoom(action);
  else sentinel('/__desktop/menu?action='+encodeURIComponent(action));
});});
bar.querySelectorAll('[data-edit]').forEach(function(item){item.addEventListener('click',function(event){
  event.stopPropagation();var command=item.dataset.edit;closeMenus(false);
  if(lastEditTarget&&lastEditTarget.isConnected&&typeof lastEditTarget.focus==='function'){
    try{lastEditTarget.focus({preventScroll:true});}catch(error){lastEditTarget.focus();}
  }
  document.execCommand(command,false,null);
});});
document.addEventListener('click',function(event){if(!bar.contains(event.target))closeMenus(false);});
var observedSidebar=null;
var sidebarResizeObserver=typeof ResizeObserver==='function'?new ResizeObserver(syncSidebarWidth):null;
// Only geometry is observed; CSS theme tokens own all chrome colors.
var lastRootVar={};
function setRootVar(name,value){
  if(!value||lastRootVar[name]===value)return;
  lastRootVar[name]=value;document.documentElement.style.setProperty(name,value);
}
function syncSidebarWidth(){
  var sidebar=document.querySelector('.jx-sider,.jx-appLoading-sidebar');
  if(sidebarResizeObserver&&sidebar&&sidebar!==observedSidebar){
    if(observedSidebar)sidebarResizeObserver.unobserve(observedSidebar);
    observedSidebar=sidebar;sidebarResizeObserver.observe(sidebar);
  }
  if(!sidebar)return;
  var rect=sidebar.getBoundingClientRect();
  var width=Math.max(0,Math.min(window.innerWidth,Math.round(rect.right)));
  setRootVar('--hugagent-desktop-sidebar-width',width+'px');
}
var chromeSyncQueued=false;
function scheduleChromeSync(){
  if(chromeSyncQueued)return;chromeSyncQueued=true;
  requestAnimationFrame(function(){chromeSyncQueued=false;syncSidebarWidth();syncLocale();});
}
syncSidebarWidth();syncLocale();
new MutationObserver(scheduleChromeSync).observe(document.documentElement,{childList:true,subtree:true,characterData:true,attributes:true,attributeFilter:['class','style','data-theme','lang']});
window.addEventListener('resize',scheduleChromeSync);
function isControl(target){return target instanceof Element&&!!target.closest('.tb-menu,.tb-controls,button');}
bar.addEventListener('mousedown',function(event){if(event.button!==0||isControl(event.target))return;sentinel('/__desktop/win?action=drag');});
bar.addEventListener('dblclick',function(event){if(isControl(event.target))return;sentinel('/__desktop/win?action=toggle-maximize');});
})();"##;

// macOS keeps application actions in the native system menu. Inside the window
// we only reserve a compact draggable title region for the traffic lights; a
// second branded toolbar would duplicate the native chrome and waste space.
const MAC_TB_CSS: &str = r##"
#hugagent-mac-titlebar{position:fixed;inset:0 0 auto 0;height:28px;z-index:2147483647;background-color:var(--module-rail-bg,var(--color-bg-layout));background-image:var(--module-rail-gradient,none);background-size:100vw 100vh;background-attachment:fixed;border:0;box-shadow:none;-webkit-user-select:none;user-select:none}
#hugagent-mac-titlebar *{box-sizing:border-box}
"##;

const MAC_TB_JS: &str = r##"(function(){
var bar=document.getElementById('hugagent-mac-titlebar');if(!bar)return;
document.documentElement.dataset.desktopPlatform='macos';
if(new URLSearchParams(location.search).get('quickask')==='1'){
  bar.remove();var style=document.getElementById('hugagent-titlebar-style');if(style)style.remove();return;
}
function isDragSurface(event){
  if(bar.contains(event.target))return true;
  if(event.target.closest('button,a,input,textarea,select,[role=button],[contenteditable],.jx-chatTopbarProject'))return false;
  return !!event.target.closest('.jx-chatTopbar,.jx-topbar');
}
document.addEventListener('mousedown',function(event){
  if(event.button!==0||!isDragSurface(event))return;
  window.location.href='/__desktop/win?action=drag';
});
document.addEventListener('dblclick',function(event){
  if(event.button!==0||!isDragSurface(event))return;
  window.location.href='/__desktop/win?action=toggle-maximize';
});
})();"##;

/// 仅交付混合模式的包没有别的运行形态可切，菜单里不摆一个点了也没意义的入口。
fn titlebar_menu_for(hybrid_only: bool) -> String {
    if !hybrid_only {
        return TB_MENU.to_string();
    }
    TB_MENU
        .lines()
        .filter(|line| {
            !["run_mode", "server_config", "local_server"]
                .iter()
                .any(|action| line.contains(&format!("data-act=\"{action}\"")))
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn titlebar_block(offset_css: &str) -> String {
    format!(
        "<style id=\"hugagent-titlebar-style\">{css}{offset}</style>\
<header id=\"hugagent-titlebar\" data-height=\"{height}\">\
<div class=\"tb-sidebarZone\">{menu}</div><div class=\"tb-mainChrome\">\
<div class=\"tb-spacer\"></div>{controls}</div></header><script>{script}</script>",
        css = format!(
            "{TB_CSS}{}",
            if offset_css == TB_OFFSET_SPA {
                SPA_CSS
            } else {
                ""
            }
        ),
        offset = offset_css,
        height = TITLEBAR_HEIGHT,
        menu = titlebar_menu_for(brand::HYBRID_ONLY),
        controls = TB_CONTROLS,
        script = TB_JS,
    )
}

fn mac_titlebar_block(offset_css: &str) -> String {
    format!(
        "<style id=\"hugagent-titlebar-style\">{css}{offset}</style>\
<header id=\"hugagent-mac-titlebar\" data-height=\"{height}\" aria-hidden=\"true\"></header><script>{script}</script>",
        css = format!("{MAC_TB_CSS}{}", if offset_css == MAC_OFFSET_SPA { SPA_CSS } else { "" }),
        offset = offset_css,
        height = MAC_TITLEBAR_HEIGHT,
        script = MAC_TB_JS,
    )
}

pub(super) fn platform_titlebar_block(spa: bool) -> String {
    if cfg!(target_os = "macos") {
        mac_titlebar_block(if spa { MAC_OFFSET_SPA } else { MAC_OFFSET_PAGE })
    } else {
        titlebar_block(if spa { TB_OFFSET_SPA } else { TB_OFFSET_PAGE })
    }
}

const SPA_CSS: &str = r##"
/* Align both desktop bars to the frontend rail's viewport-sized gradient. */
:root[data-desktop-platform] .jx-appShell,
:root[data-desktop-platform] .jx-appLoading{
  box-sizing:border-box;
  padding-top:var(--hugagent-desktop-titlebar-height);
  background-color:var(--module-rail-bg);
  background-image:var(--module-rail-gradient);
  background-size:100vw 100vh;
  background-attachment:fixed;
  overflow:hidden;
}
:root[data-desktop-platform] .jx-moduleRail{
  background-size:100vw 100vh;
  background-attachment:fixed;
}
:root[data-desktop-platform] .jx-moduleShell.ant-layout-sider,
:root[data-desktop-platform] .jx-moduleShell .ant-layout-sider-children{
  background:transparent;
}
:root[data-desktop-platform] .jx-moduleSidebar{border-top-left-radius:var(--radius-md);}
:root[data-desktop-platform] .jx-appMainLayout{
  border-top-right-radius:var(--radius-md);
  overflow:hidden;
}
:root[data-desktop-platform] .jx-appLoading::before{
  content:"";
  width:64px;
  flex:0 0 64px;
  background-color:var(--module-rail-bg);
  background-image:var(--module-rail-gradient);
  background-size:100vw 100vh;
  background-attachment:fixed;
}
:root[data-desktop-platform] .jx-appLoading-main{
  background:var(--color-bg-chat);
  border-top-right-radius:var(--radius-md);
}
:root[data-desktop-platform] .jx-appLoading-sidebar{
  box-sizing:border-box;
  width:280px;
  flex-basis:280px;
  background:var(--module-sidebar-bg);
  border-top-left-radius:var(--radius-md);
}
@media(max-width:960px){
  :root[data-desktop-platform] .jx-appLoading::before{display:none;}
  :root[data-desktop-platform] .jx-appShell > .jx-moduleShell.ant-layout-sider{
    top:var(--hugagent-desktop-titlebar-height);
    height:calc(100dvh - var(--hugagent-desktop-titlebar-height))!important;
  }
}
"##;

#[cfg(test)]
#[path = "window_chrome_tests.rs"]
mod tests;
