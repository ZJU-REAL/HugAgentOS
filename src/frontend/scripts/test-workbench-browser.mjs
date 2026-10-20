import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

const output = resolve('node_modules/.tmp/workbench-browser');
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
import './src/styles/index';
window.__workbench = {
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
const origin = 'http://127.0.0.1:5207';
const user = {user_id:'wb-user', username:'tester', lab_enabled:true, allowed_apps:null};
const sessions = [
  {chat_id:'ordinary',title:'Ordinary saved chat',created_at:'2026-10-01T10:00:00Z',metadata:{},messages:[]},
  {chat_id:'project-chat',title:'Project saved chat',created_at:'2026-10-02T10:00:00Z',project_id:'project-one',metadata:{},messages:[]},
  {chat_id:'task-chat',title:'Task saved chat',created_at:'2026-10-03T10:00:00Z',metadata:{automation_task_id:'task-one',automation_run:true},messages:[]},
];
const project = {project_id:'project-one',name:'Research project',description:'Fixture project',kind:'personal',favorite:false,owner_type:'user',created_at:'2026-10-01T10:00:00Z'};
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
      if (url.pathname.startsWith('/home/')) return route.fulfill({contentType:'image/svg+xml',body:await readFile(resolve('public',url.pathname.slice(1))).catch(()=>Buffer.from('<svg xmlns="http://www.w3.org/2000/svg"/>'))});
      return route.fulfill({contentType:'text/html',body:'<html><head><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>'});
    }
    const path=url.pathname.slice(4);
    let data = [];
    if (path==='/v1/workbench/catalog') data={origin:{kind:'cloud',id:'a'.repeat(32)},revision:'fixture-1',policy:{revision:0,defaultPinned:[],forced:[]},items:[
      ...['chat','projects','my_space','ability_center','app_center','automation','sites','lab','settings','docs'].map(id=>({
        applicationKey:'builtin:'+id,revision:1,enabled:true,declaration:{schemaVersion:'1.0.0',id,title:id,target:{kind:'builtin',componentId:id}},
        availability:{available:true,reason:null},navigation:{defaultPinned:false,forced:false}})),
      ...[['plan_mode','计划模式','enter_plan']].map(([id,title,commandId])=>({
        applicationKey:'builtin:'+id,revision:1,enabled:true,declaration:{schemaVersion:'1.0.0',id,title,target:{kind:'command',commandId}},
        availability:{available:true,reason:null},navigation:{defaultPinned:false,forced:false}}))]};
    else if (path.includes('plan_mode/launch')) data={applicationKey:'builtin:plan_mode',target:{kind:'command',commandId:'enter_plan'}};
    else if (path==='/v1/auth/session/check'  || path==='/v1/me') data=user;
    else if (path.endsWith('/preferences')) data={sidebar_layout:'modern'};
    else if (path==='/v1/meta/edition') data={edition:'ee',features:{multi_tenancy:true}};
    else if (path==='/v1/catalog') data={skills:[],agents:[],mcp:[],kb:[]};
    else if (path==='/v1/content/page-config' || path==='/v1/content/app-config') data={};
    else if (path==='/v1/content/docs') data={updates:[],capabilities:[]};
    else if (path==='/v1/chats') data={items:sessions,pagination:{page:1,page_size:100,total_items:3,has_next:false}};
    else if (path.endsWith('/messages')) {
      const id=path.split('/')[3];
      data={items:[{message_id:'message-'+id,role:'user',content:'Saved message '+id,created_at:'2026-10-02T10:00:00Z',chat_seq:1}],pagination:{has_next:false}};
    }
    else if (sessions.some(s=>path==='/v1/chats/'+s.chat_id)) data=sessions.find(s=>path.endsWith('/'+s.chat_id));
    else if (path.endsWith('/active-run')) data={active:false};
    else if (path==='/v1/projects') data={items:[project],pagination:{has_next:false}};
    else if (path==='/v1/projects/project-one') data=project;
    else if (path==='/v1/automations') data=[task];
    else if (path==='/v1/automations/task-one') data=task;
    else if (path==='/v1/automations/task-one/runs') data=[{run_id:'run-one',task_id:'task-one',chat_id:'task-chat',status:'success'}];
    else if (path==='/v1/plugins/installed') data={items:[]};
    return route.fulfill({json:{code:10000,data}});
  });
  await page.goto(origin+'/');
  await page.locator('.jx-composerEditor').waitFor();
  await page.locator('.jx-moduleSidebar').getByText('Ordinary saved chat',{exact:true}).click();
  await page.waitForURL('**/c/ordinary');
  await page.getByText('Saved message ordinary',{exact:true}).waitFor();
  await page.reload();
  await page.getByText('Saved message ordinary',{exact:true}).waitFor();
  await page.getByRole('button',{name:'更多',exact:true}).click();
  await page.getByRole('menuitem',{name:'应用中心',exact:true}).click();
  await page.waitForURL('**/app-center');
  await page.getByRole('button',{name:/计划模式/}).click();
  await page.locator('.jx-composerEditor').waitFor();
  await page.getByText('计划模式',{exact:true}).first().waitFor();
  await page.getByRole('button',{name:'更多',exact:true}).click();
  await page.getByRole('menuitem',{name:'应用中心',exact:true}).click();
  await page.goto(origin+'/c/project-chat');
  await page.getByText('Saved message project-chat',{exact:true}).waitFor();
  await page.locator('.jx-chatTopbarProject').filter({hasText:'Research project'}).waitFor();
  await page.getByRole('button',{name:'更多',exact:true}).click();
  await page.getByRole('menuitem',{name:'应用中心',exact:true}).click();
  await page.getByRole('button',{name:/计划模式/}).click();
  await page.locator('.jx-composerEditor').waitFor();
  await page.locator('[data-panel="chat"]').getByText('Research project',{exact:true}).last().waitFor();

  await page.goto(origin+'/automation/task-one/conversations/task-chat');
  await page.getByText('Saved message task-chat',{exact:true}).waitFor();
  await page.locator('.jx-taskSidebar .jx-historyItem.active').waitFor();
  await page.goto(origin+'/projects');
  await page.locator('[data-panel="projects"]').getByText('Research project',{exact:true}).waitFor();
  await page.goto(origin+'/settings');
  await page.locator('[data-panel="settings"]').getByText('个人信息',{exact:true}).first().waitFor();
  await page.goBack();
  await page.locator('[data-panel="projects"]').getByText('Research project',{exact:true}).waitFor();
  await page.evaluate(()=>window.__workbench.layout('classic'));
  await page.locator('.jx-classicShell').waitFor();
  await page.locator('.jx-classicNav').getByRole('button',{name:'定时任务',exact:true}).click();
  await page.locator('.jx-taskSidebar').getByText('Task saved chat',{exact:true}).click();
  await page.waitForURL('**/automation/task-one/conversations/task-chat');
  await page.getByText('Saved message task-chat',{exact:true}).waitFor();
  await page.evaluate(()=>window.__workbench.availability(false,true));
  await page.getByRole('button',{name:'更多',exact:true}).click();
  assert.deepEqual(await page.getByRole('menu').getByRole('menuitem').allTextContents(),['项目','应用中心']);
  assert.equal(await page.locator('.jx-classicNav').getByRole('button',{name:'我的空间',exact:true}).count(),0);
  await page.keyboard.press('Escape');
  await page.evaluate(()=>window.__workbench.layout('modern'));
  await page.locator('.jx-moduleShell').waitFor();
  assert.equal(await page.getByRole('button',{name:'我的空间',exact:true}).count(),0);
  await page.evaluate(()=>{window.__workbench.permissions([]);window.__workbench.open('/automation');});
  await page.getByRole('alert').getByText('应用不存在或你没有访问权限',{exact:true}).waitFor();
  assert.equal(await page.locator('.jx-moduleRail').getByRole('button',{name:'定时任务',exact:true}).count(),0);
  await page.screenshot({path:resolve(output,'registered-workspace.png')});
  assert.deepEqual(errors,[],'registered pages have no browser runtime exceptions');
  console.log('workbench browser: real App/workspace/pages; home, recovery, project, automation, commands, old routes, both layouts (API fixtures) passed');
} catch(error) {
  await page.screenshot({path:resolve(output,'failure.png')});
  console.error('URL',page.url(),'ERRORS',errors,'UI',(await page.locator('body').innerText()).slice(-3500));
  throw error;
} finally {await browser.close();}
