import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const output = resolve('node_modules/.tmp/desktop-chrome');
await mkdir(output, { recursive: true });
const rust = await readFile(resolve('../../desktop/src-tauri/src/window_chrome.rs'), 'utf8');
const raw = name => rust.match(new RegExp('const '+name+': &str = r##"([\\s\\S]*?)"##;'))?.[1] || '';
const offset = name => rust.match(new RegExp('const '+name+': &str =\\s*"([^"]*)";'))?.[1] || raw(name);
await build({stdin:{contents:`
import { createRoot } from 'react-dom/client';
import { Layout } from 'antd';
import { createBrowserRouter,RouterProvider } from 'react-router';
import { AppThemeProvider } from './src/AppThemeProvider';
import { Sidebar } from './src/components/sidebar/Sidebar';
import { AppLoadingSkeleton } from './src/components/common/AppLoadingSkeleton';
import { bindRouter } from './src/routing/navigation';
import { useUIStore,useAuthStore,useCatalogStore,useChatStore,useEditionStore } from './src/stores';
import './src/styles/index';
useAuthStore.setState({authUser:{user_id:'fixture',username:'tester'},authChecking:false});
useEditionStore.setState({loaded:true,edition:'ee',features:{}});
useCatalogStore.setState({catalog:{skills:[],mcp:[],agents:[],kb:[]},catalogLoading:false});
useChatStore.setState({currentUserId:'fixture',chatsLoading:false});
const actions={onNewChat:()=>{document.documentElement.dataset.chatClicked='yes'},onNewProjectChat:()=>{},onDeleteChat:()=>{},onTogglePinned:()=>{},onToggleFavorite:()=>{},onStartRename:()=>{},onCommitRename:()=>{},onExportChat:()=>{},onSelectChat:()=>{},onSetPanel:(p,s)=>useCatalogStore.getState().setPanel(p,s)};
function Workspace(){return <Layout className="jx-appShell" style={{height:'100vh'}}><Sidebar {...actions}/><Layout className="jx-appMainLayout"><div className="jx-primaryPane is-chatSurface"><header className="jx-chatTopbar">桌面工作区</header><div style={{flex:1,padding:32}}>聊天与工作内容</div></div></Layout></Layout>}
const router=createBrowserRouter([{path:'*',element:<Workspace/>}]);bindRouter(router);
const root=createRoot(document.getElementById('root'));
window.__fixture={theme:mode=>useUIStore.getState().setThemeMode(mode),collapse:v=>useUIStore.getState().setSiderCollapsed(v),loading:()=>root.render(<AppThemeProvider><AppLoadingSkeleton/></AppThemeProvider>)};
root.render(<AppThemeProvider><RouterProvider router={router}/></AppThemeProvider>);
`,resolveDir:process.cwd(),loader:'tsx'},outfile:resolve(output,'fixture.js'),bundle:true,jsx:'automatic',format:'esm',define:{'import.meta.env':'{"VITE_DEFAULT_LANGUAGE":"zh-CN"}'},loader:{'.css':'css','.svg':'dataurl','.woff2':'dataurl','.woff':'dataurl','.ttf':'dataurl'},external:['/loader.gif','/loader-done.png']});
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM});
try{
 for(const platform of ['windows','macos']){
  const mac=platform==='macos',height=mac?28:34;
  const page=await browser.newPage({viewport:{width:1440,height:900},locale:'zh-CN'});
  await page.addInitScript(() => localStorage.setItem('jx_lang', 'zh-CN'));
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const markup=mac?'<header id="hugagent-mac-titlebar"></header>':'<header id="hugagent-titlebar"><div class="tb-sidebarZone">'+raw('TB_MENU')+'</div><div class="tb-mainChrome"><div class="tb-spacer"></div>'+raw('TB_CONTROLS')+'</div></header>';
  const css=raw(mac?'MAC_TB_CSS':'TB_CSS')+offset(mac?'MAC_OFFSET_SPA':'TB_OFFSET_SPA')+raw('SPA_CSS');
  await page.route('http://desktop.test/**',async route=>{
   const path=new URL(route.request().url()).pathname;
   if(path==='/fixture.js'||path==='/fixture.css')return route.fulfill({contentType:path.endsWith('.css')?'text/css':'text/javascript',body:await readFile(resolve(output,path.slice(1)))});
   if(path.startsWith('/api/'))return route.fulfill({json:{code:10000,data:[]}});
   if(path.startsWith('/home/')){
    const body=await readFile(resolve('public',path.slice(1))).catch(()=>null);
    return body?route.fulfill({contentType:path.endsWith('.svg')?'image/svg+xml':'image/png',body}):route.fulfill({status:404});
   }
   return route.fulfill({contentType:'text/html',body:'<html><head><meta charset="utf-8"><link rel="stylesheet" href="/fixture.css"><style>'+css+'</style></head><body>'+markup+'<script>'+raw(mac?'MAC_TB_JS':'TB_JS')+'</script><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>'});
  });
  await page.goto('http://desktop.test/');
  await page.locator('.jx-moduleRail').waitFor();
  for(const theme of ['light','dark']){
   await page.evaluate(mode=>window.__fixture.theme(mode),theme);
   await page.waitForFunction(mode=>(document.documentElement.dataset.theme||'light')===mode,theme);
   for(const collapsed of [false,true]){
    await page.evaluate(v=>window.__fixture.collapse(v),collapsed);
    await page.waitForTimeout(250);
    const surfaces=await page.evaluate(mac=>{
     const get=selector=>{const el=document.querySelector(selector);if(!el)return null;const s=getComputedStyle(el),r=el.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,bottom:r.bottom,color:s.backgroundColor,image:s.backgroundImage,size:s.backgroundSize,attachment:s.backgroundAttachment};};
     return {bar:get(mac?'#hugagent-mac-titlebar':'#hugagent-titlebar'),rail:get('.jx-moduleRail'),sidebar:get('.jx-moduleSidebar'),main:get('.jx-appMainLayout'),pane:get('.jx-primaryPane'),scroll:document.documentElement.scrollWidth,viewport:innerWidth};
    },mac);
    assert.equal(surfaces.rail.y,height,platform+' navigation clears window controls');
    assert.equal(surfaces.main.y,height,platform+' content clears window controls');
    for(const key of ['color','image','size','attachment'])assert.equal(surfaces.bar[key],surfaces.rail[key],'chrome and rail share '+key);
    assert.equal(surfaces.rail.width,64,'compact navigation rail');
    assert.equal(surfaces.main.x,collapsed?64:344,'content follows the compact rail and secondary sidebar');
    const button=await page.locator('.jx-moduleButton').first().boundingBox();
    assert.equal(button.width,44,'navigation retains its click target');
    assert.ok(button.x>=surfaces.rail.x && button.x+button.width<=surfaces.rail.x+surfaces.rail.width,'navigation button fits inside rail');
    if(!collapsed){assert.notEqual(surfaces.sidebar.color,surfaces.rail.color);assert.notEqual(surfaces.sidebar.color,surfaces.pane.color);}
    assert.ok(surfaces.scroll<=surfaces.viewport,'no horizontal overflow');
    await page.screenshot({path:resolve(output,platform+'-'+theme+'-'+(collapsed?'collapsed':'expanded')+'.png'),animations:'disabled'});
   }
  }
  await page.evaluate(()=>{window.__fixture.collapse(false);window.__fixture.theme('light');});
  await page.getByRole('button',{name:'新建对话',exact:true}).click();
  assert.equal(await page.locator('html').getAttribute('data-chat-clicked'),'yes');
  if(!mac){await page.getByRole('button',{name:'文件',exact:true}).click();await page.locator('#hugagent-file-menu').waitFor({state:'visible'});await page.keyboard.press('Escape');await page.locator('#hugagent-file-menu').waitFor({state:'hidden'});}
  await page.setViewportSize({width:760,height:800});
  await page.evaluate(()=>window.__fixture.collapse(false));await page.waitForTimeout(250);
  assert.equal((await page.locator('.jx-moduleRail').boundingBox()).y,height,'narrow navigation clears chrome');
  await page.evaluate(()=>window.__fixture.collapse(true));await page.waitForTimeout(250);
  assert.equal(await page.locator('.jx-moduleRail').evaluate(el=>el.getBoundingClientRect().right<=0),true,'narrow collapsed navigation is offscreen');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.screenshot({path:resolve(output,platform+'-narrow.png'),animations:'disabled'});
  await page.setViewportSize({width:1440,height:900});
  const contentColor=await page.locator('.jx-primaryPane').evaluate(el=>getComputedStyle(el).backgroundColor);
  await page.evaluate(()=>window.__fixture.loading());await page.locator('.jx-appLoading').waitFor();
  assert.equal(await page.locator('.jx-appLoading').evaluate(el=>getComputedStyle(el).paddingTop),height+'px','loading keeps safe area');
  const loading=await page.evaluate(()=>{
   const shell=document.querySelector('.jx-appLoading'),main=document.querySelector('.jx-appLoading-main'),sidebar=document.querySelector('.jx-appLoading-sidebar');
   return {main:getComputedStyle(main).backgroundColor,rail:getComputedStyle(shell,'::before').width,sidebar:sidebar.getBoundingClientRect().width};
  });
  assert.equal(loading.rail,'64px','loading reserves the same outer rail');
  assert.equal(loading.sidebar,280,'loading secondary navigation aligns with workspace');
  assert.equal(loading.main,contentColor,'loading preserves content surface');
  await page.screenshot({path:resolve(output,platform+'-loading.png'),animations:'disabled'});
  assert.deepEqual(errors,[]);await page.close();
 }
 console.log('Desktop chrome: shared colors/gradient, themes, collapse, safe areas, menus, narrow layout and loading passed');
}finally{await browser.close();}
