(function(){
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
})();