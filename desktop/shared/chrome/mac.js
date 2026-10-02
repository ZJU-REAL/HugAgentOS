(function(){
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
})();