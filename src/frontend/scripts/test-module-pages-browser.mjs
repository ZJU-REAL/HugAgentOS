import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

const output = resolve('node_modules/.tmp/module-pages');
await mkdir(output, { recursive: true });
await build({plugins:[{name:'worklet-url',setup(b){b.onResolve({filter:/\?url$/},()=>({path:'worklet',namespace:'voice'}));b.onLoad({filter:/.*/,namespace:'voice'},()=>({contents:'export default "/capture.worklet.js"',loader:'js'}));}}],
  stdin: { contents: `
import { createRoot } from 'react-dom/client';
import { RouterProvider } from 'react-router';
import { AppThemeProvider } from './src/AppThemeProvider';
import { createAppRouter } from './src/routing/routes';
import { bindRouter, navigateTo } from './src/routing/navigation';
import { useSidebarLayoutStore } from './src/stores/sidebarLayoutStore';
import { useAuthStore } from './src/stores/authStore';
import { useDeploymentModeStore } from './src/stores/deploymentModeStore';
import { useCanvasStore } from './src/stores/canvasStore';
import { useAutomationChatStore } from './src/stores/automationChatStore';
import { useChatStore } from './src/stores/chatStore';
import './src/styles/index';
window.__workbench = {
  preview: () => useCanvasStore.getState().openCanvas({file_id:'report',name:'Report.txt',url:'/files/report',mime_type:'text/plain'}),
  automationSelection: () => {
    const state = useAutomationChatStore.getState();
    return {taskId:state.activeGroup?.taskId,runId:state.selectedRunId,chatId:useChatStore.getState().currentChatId};
  },
  permissions: apps => useAuthStore.setState({authUser:{...useAuthStore.getState().authUser,allowed_apps:apps}}),
  open: path => navigateTo(path),
  layout: layout => useSidebarLayoutStore.setState({layout}),
  availability: (lab, local) => {
    useAuthStore.setState({authUser: {...useAuthStore.getState().authUser, lab_enabled: lab}});
    useDeploymentModeStore.setState({provisionMode: local ? 'local_only' : 'web'});
  },
};
const router = createAppRouter(); bindRouter(router);
createRoot(document.getElementById('root')).render(<AppThemeProvider><RouterProvider router={router}/></AppThemeProvider>);
`, resolveDir: process.cwd(), loader: 'tsx' },
  alias:{react:resolve('node_modules/react'),'react-dom':resolve('node_modules/react-dom')},outdir:output,entryNames:'fixture',splitting:true, bundle: true, format: 'esm', jsx: 'automatic',
  define: { 'import.meta.env': '{}' },
  loader: { '.css': 'css', '.svg': 'dataurl', '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl' },
  external: ['/loader.gif', '/loader-done.png'],
});
const origin = 'http://127.0.0.1:5307';
const user = {user_id:'wb-user', username:'tester', lab_enabled:true, allowed_apps:null};
const sessions = [
  {chat_id:'ordinary',title:'Ordinary saved chat',created_at:'2026-10-01T10:00:00Z',metadata:{},messages:[]},
  {chat_id:'project-chat',title:'Project saved chat',created_at:'2026-10-02T10:00:00Z',project_id:'project-one',metadata:{},messages:[]},
  {chat_id:'task-chat',title:'Task saved chat',created_at:'2026-10-03T10:00:00Z',metadata:{automation_task_id:'task-one',automation_run:true},messages:[]},
];
const project = {project_id:'project-one',name:'Research project',description:'Fixture project',kind:'personal',favorite:false,owner_type:'user',created_at:'2026-10-01T10:00:00Z'};
const projects = Array.from({length: 36}, (_, n) => ({...project, project_id: 'project-' + n, name: 'Project ' + n, description: 'A page scrolling regression project'}));
const files = Array.from({length: 60}, (_, n) => ({name:'Document '+n+'.txt',path:'Document '+n+'.txt',is_directory:false,size:100,modified_at:'2026-10-01T10:00:00Z'}));
const space = {space_id:'personal',name:'Personal files',kind:'personal',permission:'admin',mount_path:'/personal'};
const apps = Array.from({length: 30}, (_, n) => ({applicationKey:'external:app_'+n,revision:1,enabled:true,declaration:{schemaVersion:'1.0.0',id:'app_'+n,title:'Scenario '+n,description:'Scenario application '+n,target:{kind:'external',mode:'browser',url:'https://example.test/app/'+n}},availability:{available:true,reason:null},navigation:{defaultPinned:false,forced:false}}));
const task = {task_id:'task-one',name:'Daily report',status:'active',task_type:'prompt',schedule_type:'recurring',cron_expression:'0 9 * * *'};
const browser = await chromium.launch({headless:true});
const page = await browser.newPage({viewport:{width:1440,height:1000}});
const errors = [];
page.on('pageerror',e => errors.push(e.message));
await page.addInitScript(() => localStorage.setItem('jx_lang','zh-CN'));
try {
  await page.route(origin+'/**',async route => {
    const url = new URL(route.request().url());
    if ((url.pathname.endsWith('.js')||url.pathname.endsWith('.css'))) return route.fulfill({
      contentType:url.pathname.endsWith('.css')?'text/css':'text/javascript',
      body:await readFile(resolve(output,url.pathname.slice(1))),
    });
    if (!url.pathname.startsWith('/api/')) {
      if (url.pathname.startsWith('/home/') || url.pathname.startsWith('/icons/')) {
        const body = await readFile(resolve('public',decodeURIComponent(url.pathname.slice(1)))).catch(()=>null);
        return body ? route.fulfill({contentType:url.pathname.endsWith('.svg')?'image/svg+xml':'image/png',body}) : route.fulfill({status:404});
      }
      return route.fulfill({contentType:'text/html',body:'<html><head><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>'});
    }
    const path=url.pathname.slice(4);
    if (path==='/files/report') return route.fulfill({contentType:'text/plain',body:'Scheduled report preview'});
    let data = [];
    if (path==='/v1/file/report/text') data={content:'Scheduled report preview',revision:'fixture-1',can_edit:false};
    else if (path==='/v1/workbench/catalog') data={origin:{kind:'cloud',id:'a'.repeat(32)},revision:'fixture-1',policy:{revision:0,defaultPinned:[],forced:[]},items:[
      ...['chat','projects','my_space','ability_center','app_center','automation','sites','lab','settings','docs'].map(id=>({
        applicationKey:'builtin:'+id,revision:1,enabled:true,declaration:{schemaVersion:'1.0.0',id,title:id,target:{kind:'builtin',componentId:id}},
        availability:{available:true,reason:null},navigation:{defaultPinned:false,forced:false}})),
      ...[['plan_mode','计划模式','enter_plan']].map(([id,title,commandId])=>({
        applicationKey:'builtin:'+id,revision:1,enabled:true,declaration:{schemaVersion:'1.0.0',id,title,target:{kind:'command',commandId}},
        availability:{available:true,reason:null},navigation:{defaultPinned:false,forced:false}})),...apps]};
    else if (path.includes('plan_mode/launch')) data={applicationKey:'builtin:plan_mode',target:{kind:'command',commandId:'enter_plan'}};
    else if (path==='/v1/auth/session/check'  || path==='/v1/me') data=user;
    else if (path.endsWith('/preferences')) data={sidebar_layout:'modern'};
    else if (path==='/v1/meta/edition') data={edition:'ee',features:{multi_tenancy:true}};
    else if (path==='/v1/catalog') data={skills:Array.from({length:36},(_,n)=>({id:'skill-'+n,name:'Skill '+n,desc:'Useful skill '+n,enabled:true,owner:'self'})),agents:[],mcp:[],kb:[]};
    else if (path==='/v1/content/page-config' || path==='/v1/content/app-config') data={};
    else if (path==='/v1/content/docs') data={updates:Array.from({length:36},(_,n)=>({date:'10.18',year:'2026',title:'Update '+n,category:'体验优化',desc:'A useful product update with a clear explanation.'})),capabilities:[]};
    else if (path==='/v1/chats') data={items:sessions,pagination:{page:1,page_size:100,total_items:3,has_next:false}};
    else if (path.endsWith('/messages')) {
      const id=path.split('/')[3];
      data={items:[{message_id:'message-'+id,role:'user',content:'Saved message '+id,created_at:'2026-10-02T10:00:00Z',chat_seq:1}],pagination:{has_next:false}};
    }
    else if (sessions.some(s=>path==='/v1/chats/'+s.chat_id)) data=sessions.find(s=>path.endsWith('/'+s.chat_id));
    else if (path.endsWith('/active-run')) data={active:false};
    else if (path==='/v1/projects') data={items:projects,pagination:{has_next:false}};
    else if (path==='/v1/projects/project-one') data=project;
    else if (path==='/v1/automations') data=Array.from({length:36},(_,n)=>({...task,task_id:'task-'+n,name:'Scheduled report '+n}));
    else if (path==='/v1/automations/task-one') data=task;
    else if (path==='/v1/automations/task-one/runs') data=[{run_id:'run-one',task_id:'task-one',chat_id:'task-chat',status:'success'}];
    else if (path==='/v1/myspace/mounts') data={items:[space],revision:'1'};
    else if (path==='/v1/myspace/entries') data={space,path:'',entries:files,next_cursor:null};
    else if (path==='/v1/sites') data={items:Array.from({length:36},(_,n)=>({site_id:'site-'+n,title:'Site '+n,slug:'site-'+n,status:'published',url:'https://example.test/site/'+n,visibility:'private',created_at:'2026-10-01T10:00:00Z'})),pagination:{total_items:36}};
    else if (path==='/v1/applications') data={items:[]};
    else if (path==='/v1/plugins/installed') data={items:[]};
    return route.fulfill({json:{code:10000,data}});
  });
  await page.goto(origin + '/');
  await page.locator('.jx-composerEditor').waitFor();
  await page.getByText('Ordinary saved chat', {exact:true}).waitFor();
  await page.evaluate(() => window.__workbench.open('/automation/task-one/conversations/task-chat'));
  await page.locator('.jx-runTimeline').waitFor();
  const selection = {taskId:'task-one',runId:'run-one',chatId:'task-chat'};
  assert.deepEqual(await page.evaluate(() => window.__workbench.automationSelection()), selection);
  for (const width of [1920, 1280]) {
    await page.setViewportSize({width, height:900});
    for (const fullscreen of [false, true]) {
      await page.evaluate(() => window.__workbench.preview());
      await page.locator('.jx-canvas').waitFor();
      assert.equal(await page.locator('.jx-runTimeline').count(), 0, 'Canvas must remove the timeline from layout immediately');
      if (fullscreen) {
        await page.getByRole('button',{name:'全屏',exact:true}).click();
        await page.getByRole('button',{name:'退出全屏',exact:true}).waitFor();
      }
      await page.getByText('Scheduled report preview', {exact:true}).waitFor();
      assert.deepEqual(await page.evaluate(() => window.__workbench.automationSelection()), selection);
      assert.equal(new URL(page.url()).pathname, '/automation/task-one/conversations/task-chat');
      await page.screenshot({path:resolve(output,'automation-canvas-'+width+'-'+fullscreen+'.png'),animations:'disabled'});
      await page.getByRole('button',{name:'收起右侧面板',exact:true}).click();
      await page.locator('.jx-canvas').waitFor({state:'detached'});
      await page.locator('.jx-runTimeline').waitFor();
      assert.deepEqual(await page.evaluate(() => window.__workbench.automationSelection()), selection);
    }
  }
  await page.evaluate(() => window.__workbench.preview());
  await page.locator('.jx-canvas').waitFor();
  await page.evaluate(() => window.__workbench.open('/c/ordinary'));
  await page.locator('.jx-canvas').waitFor({state:'detached'});
  await page.locator('.jx-runTimeline').waitFor({state:'detached'});
  assert.deepEqual(await page.evaluate(() => window.__workbench.automationSelection()), {taskId:undefined,runId:null,chatId:'ordinary'});
  await page.evaluate(() => window.__workbench.open('/automation/task-one/conversations/task-chat'));
  await page.locator('.jx-runTimeline').waitFor();
  assert.deepEqual(await page.evaluate(() => window.__workbench.automationSelection()), selection);
  console.log('automation Canvas: timeline hides during preview and fullscreen, then returns with selection preserved at two desktop widths');
  if (!process.argv.includes('--scroll-only')) {
  await page.evaluate(() => window.__workbench.open('/app-center'));
  const center = page.locator('.jx-agentPage');
  await center.locator('[data-application-open="builtin:plan_mode"]').waitFor();
  const keys = await center.locator('[data-application-open]').evaluateAll(nodes => nodes.map(n => n.dataset.applicationOpen));
  assert.deepEqual(keys, ['builtin:plan_mode', ...apps.map(app => app.applicationKey)], 'App Center contains mode commands and concrete applications, never shell modules');
  assert.equal(await center.locator('.jx-agentPage-grid .jx-agentCard').count(), 31, 'historical card grid is restored');
  assert.equal(await center.getByRole('searchbox').count(), 0, 'App Center retains the original card-only presentation');
  await page.screenshot({path:resolve(output,'app-center-top.png'),animations:'disabled'});
  await center.locator('[data-application-open="builtin:plan_mode"]').focus();
  await page.keyboard.press('Enter');
  await page.locator('.jx-composerEditor').waitFor();
  await page.getByText('计划模式', {exact:true}).first().waitFor();

  }
  for (const layout of ['modern', 'classic']) {
   await page.evaluate(layout => window.__workbench.layout(layout), layout);
   for (const width of [1920, 1280, 390]) {
    await page.setViewportSize({width, height:720});
    for (const [path, ready] of [['/projects','.jx-projectCard-title'],['/my-space/assets','.jx-mySpace-docRow-name'],['/sites','.jx-sites-cardTitle'],['/app-center','.jx-agentCard'],['/ability-center/skills','.jx-sk-cardName'],['/automation','.jx-automation-card-name'],['/settings/session','.jx-layoutChoices'],['/help','.jx-tlTitle'],['/lab','.jx-labPage .jx-agentCard']]) {
     await page.evaluate(path => window.__workbench.open(path), path);
     const panel = path === '/help' ? 'docs' : path.split('/')[1].replaceAll('-', '_');
     await page.locator('.jx-panel[data-panel="'+panel+'"] '+ready).first().waitFor();
     await page.waitForURL('**'+path);
     await page.locator('.jx-panel').evaluate(async node => {
       await Promise.all(node.getAnimations({subtree:true}).filter(animation=>animation.effect?.getTiming().iterations!==Infinity).map(animation=>animation.finished.catch(()=>{})));
     });
     await page.evaluate(() => new Promise(requestAnimationFrame));
     const metrics = await page.locator('.jx-content').evaluate(content => {
      const visible = node => {const r=node.getBoundingClientRect();return r.width>0&&r.height>0;};
      const owners = [content,...content.querySelectorAll('*')].filter(node => visible(node) && /auto|scroll/.test(getComputedStyle(node).overflowY) && node.scrollHeight>node.clientHeight+2);
      const pane = content.getBoundingClientRect();
      return {right:pane.right, viewport:innerWidth, pageOverflow:document.documentElement.scrollWidth>innerWidth+1,
       owners:owners.map(node=>({className:node.className,right:node.getBoundingClientRect().right,height:node.clientHeight,scroll:node.scrollHeight}))};
     });
     assert.equal(metrics.pageOverflow,false,path+' has no horizontal page overflow');
     if (path==='/lab' && !metrics.owners.length) continue;
     assert.equal(metrics.owners.length,1,path+' has one page scroll owner: '+JSON.stringify(metrics));
     assert.ok(Math.abs(metrics.owners[0].right-metrics.right)<2,path+' scrollbar belongs at page edge: '+JSON.stringify(metrics));
     const reading = path==='/projects' ? ['.jx-projects-shell',792] : path.startsWith('/my-space') ? ['.jx-mySpace-shell',980] : ['/sites','/app-center','/automation'].includes(path) ? ['.jx-agentPage',840] : null;
     if (reading) {
       const column = await page.locator(String(reading[0])).boundingBox();
       const pane = await page.locator('.jx-content').boundingBox();
       assert.ok(column.width <= Number(reading[1])+1,path+' keeps its reading width');
       assert.ok(Math.abs(column.x+column.width/2-(pane.x+pane.width/2))<2,path+' keeps content centered');
     }
     const scroller = page.locator('.jx-content');
     const before = await scroller.evaluate(node=>node.scrollTop);
     assert.ok(before < 1,path+' begins at top after module navigation');
     const box = await scroller.boundingBox();
     await page.mouse.move(box.x+box.width-30,box.y+box.height/2);
     await page.mouse.wheel(0,500);
     await page.waitForFunction(before=>document.querySelector('.jx-content').scrollTop>before,before);
     if (width===1920 && layout==='modern') await page.screenshot({path:resolve(output,path.split('/')[1]+'.png'),animations:'disabled'});
    }
   }
  }
  assert.deepEqual(errors,[]);
  console.log('module pages: app-only cards and plan keyboard launch; one full-width scroll viewport, wheel interaction and no overflow for nine modules, two layouts, three widths passed');
} catch(error) {
 await page.screenshot({path:resolve(output,'failure.png'),animations:'disabled'});
 console.error('URL',page.url(),'ERRORS',errors,'UI',(await page.locator('body').innerText()).slice(-2000));
 throw error;
} finally {await browser.close();}
