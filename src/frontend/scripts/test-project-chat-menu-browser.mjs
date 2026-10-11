import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';
import { mkdir } from 'node:fs/promises';
const bundle = await build({ stdin: { contents: `
import { createRoot } from 'react-dom/client';
import ModelEffortChip from './src/components/chat/ModelEffortChip';
import { useChatStore, projectOverviewId } from './src/stores/chatStore';
import { useProjectStore } from './src/stores/projectStore';
import { useModelCapabilitiesStore } from './src/stores/modelCapabilitiesStore';
import { ProjectSidebarGroup } from './src/components/sidebar/ProjectSidebarGroup';
import { useRef } from 'react';
import { BrowserRouter } from 'react-router';
import './src/styles/variables.css';
import './src/styles/chat.css';
useChatStore.setState({currentUserId:'fixture'});
useProjectStore.setState({list:[{project_id:'p1',name:'Project One',kind:'personal'}]});
useModelCapabilitiesStore.setState({capabilities:{supports_reasoning_effort:true,user_model_switch_enabled:true,
user_selectable_models:Array.from({length:60},(_,i)=>({provider_id:'m'+i,display_name:'Model '+i,provider:'fixture',model_name:'m'+i,is_default:i===0}))}});
window.addModels = () => {
 const state=useModelCapabilitiesStore.getState();
 useModelCapabilitiesStore.setState({capabilities:{...state.capabilities,user_selectable_models:
 [...state.capabilities.user_selectable_models,...Array.from({length:140},(_,i)=>({
 provider_id:'m'+(i+60),display_name:'Model '+(i+60),provider:'fixture',model_name:'m'+(i+60)
 }))]}});
};
function Fixture() {
 const chat=useChatStore(), current=chat.store.chats[chat.currentChatId], drag=useRef(null);
 return <>
 <ProjectSidebarGroup pg={{projectId:'p1',name:'Project One',known:true,items:[],canAdmin:true,canDelete:true}}
 actions={{onNewProjectChat:(projectId,projectName)=>chat.newChat({projectId,projectName})}} dragScopeRef={drag}/>
 <button onClick={()=>useProjectStore.getState().openProject('p1')}>Open project panel</button>
 <button onClick={()=>chat.updateMessages(chat.currentChatId,[{role:'user',uid:'sent',content:'Sent',ts:1}])}>Send first message</button>
 <div data-testid="surface">{projectOverviewId(current)?'project panel':'normal conversation'}</div>
 <div data-testid="project">{current?.projectId}</div>
 <div id="anchor" style={{position:'absolute',top:130,right:20}}><ModelEffortChip/></div>
 <button id="outside" style={{position:'absolute',bottom:10,left:10}}>Outside</button>
 </>;
}
createRoot(document.getElementById('root')).render(<BrowserRouter><Fixture/></BrowserRouter>);
`, resolveDir:process.cwd(),loader:'tsx'},bundle:true,write:false,format:'esm',jsx:'automatic',
outfile:'fixture.js',define:{'import.meta.env':'{}'},loader:{'.css':'css','.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl'}});
const js=bundle.outputFiles.find(f=>f.path.endsWith('.js')).text;
const css=bundle.outputFiles.find(f=>f.path.endsWith('.css'))?.text||'';
const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
try {
 const page=await browser.newPage({viewport:{width:1000,height:700}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',route=>route.fulfill({json:{code:0,data:{items:[]}}}));
 await page.goto('http://project-menu.test');
 await page.setContent('<style>'+css+'</style><div id="root"></div>');
 await page.addScriptTag({content:js,type:'module'});
 if(process.argv[2]!=='menu'){
  await page.getByRole('button',{name:'新建项目对话',exact:true}).click();
  assert.equal(await page.getByTestId('surface').innerText(),'normal conversation','new project chat uses normal conversation');
  assert.equal(await page.getByTestId('project').innerText(),'p1','project context stays selected');
  await page.getByRole('button',{name:'Open project panel'}).click();
  await page.getByTestId('surface').getByText('project panel',{exact:true}).waitFor();
  await page.getByRole('button',{name:'新建项目对话',exact:true}).click();
  assert.equal(await page.getByTestId('surface').innerText(),'normal conversation','new chat exits overview');
  await page.getByRole('button',{name:'Open project panel'}).click();
  await page.getByTestId('surface').getByText('project panel',{exact:true}).waitFor();
  await page.getByRole('button',{name:'Send first message'}).click();
  assert.equal(await page.getByTestId('surface').innerText(),'normal conversation','sending exits overview');
 }
 const openModels=async()=>{
  await page.getByRole('button',{name:'切换模型与思考强度',exact:true}).click();
  await page.getByRole('menuitem',{name:/模型/}).click();
 };
 const bounded=async()=>{
  const menu=page.getByRole('menu');
  await menu.waitFor(); await page.waitForTimeout(250);
  const r=await menu.boundingBox(),v=page.viewportSize();
  const titlebar=await page.evaluate(()=>parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--hugagent-desktop-titlebar-height'))||0);
  assert.ok(r.y>=titlebar&&r.y+r.height<=v.height+1,'vertical viewport: '+JSON.stringify(r));
  assert.ok(r.x>=0&&r.x+r.width<=v.width+1,'horizontal viewport: '+JSON.stringify(r));
  assert.equal(await page.locator('.jx-menuPaneList').evaluate(e=>e.scrollHeight>e.clientHeight),true,'long list scrolls');
 };
 await openModels();await bounded();
 await mkdir('node_modules/.tmp/project-chat-menu',{recursive:true});
 await page.screenshot({path:'node_modules/.tmp/project-chat-menu/top.png'});
 const last=page.getByRole('menuitemradio',{name:'Model 59',exact:true});
 await last.scrollIntoViewIfNeeded();await last.click();
 await page.getByRole('menu').waitFor({state:'hidden'});
 assert.match(await page.getByRole('button',{name:'切换模型与思考强度',exact:true}).innerText(),/Model 59/);
 await page.setViewportSize({width:380,height:300});await openModels();await bounded();
 await page.setViewportSize({width:320,height:240});await bounded();
 await page.screenshot({path:'node_modules/.tmp/project-chat-menu/small.png'});
 await page.keyboard.press('Escape');await page.getByRole('menu').waitFor({state:'hidden'});
 await page.getByRole('button',{name:'Open project panel'}).click();
 await page.getByTestId('surface').getByText('project panel',{exact:true}).waitFor();
 await page.setViewportSize({width:1000,height:650});
 await page.evaluate(()=>document.getElementById('anchor').style.top='450px');
 await openModels();await bounded();
 const projectMenu=await page.getByRole('menu').boundingBox();
 const projectTrigger=await page.getByRole('button',{name:'切换模型与思考强度',exact:true}).boundingBox();
 assert.ok(projectMenu.y >= projectTrigger.y+projectTrigger.height,'project menu must open down even with more room above');
 await page.evaluate(()=>window.addModels());
 await bounded();
 assert.equal(await page.getByRole('menuitemradio').count(),200,'new model entries use the same bounded menu');
 const expandedMenu=await page.getByRole('menu').boundingBox();
 assert.ok(expandedMenu.y>=projectTrigger.y+projectTrigger.height,'new models cannot flip project menu upward');
 // Moving the project composer keeps the same direction.
 await page.evaluate(()=>{document.getElementById('anchor').style.paddingTop='20px';});
 await bounded();
 const movedMenu=await page.getByRole('menu').boundingBox();
 const movedTrigger=await page.getByRole('button',{name:'切换模型与思考强度',exact:true}).boundingBox();
 assert.ok(movedMenu.y>=movedTrigger.y+movedTrigger.height,'project menu stays downward after layout change: '+JSON.stringify({movedMenu,movedTrigger}));
 await page.getByRole('menu').getByRole('button',{name:/模型/}).click();
 await page.getByRole('menuitem',{name:/思考强度/}).click();
 const effortMenu=await page.getByRole('menu').boundingBox();
 assert.ok(effortMenu.y>=movedTrigger.y+movedTrigger.height,'effort pane follows the project downward policy');
 await page.keyboard.press('Escape');await page.getByRole('menu').waitFor({state:'hidden'});
 await page.setViewportSize({width:1000,height:430});
 await page.evaluate(()=>{
  document.documentElement.style.setProperty('--hugagent-desktop-titlebar-height','40px');
  document.getElementById('anchor').style.top='200px';
 });
 await openModels();await bounded();
 await page.keyboard.press('Escape');await page.getByRole('menu').waitFor({state:'hidden'});
 await page.getByRole('button',{name:'新建项目对话',exact:true}).click();
 await page.setViewportSize({width:1000,height:700});
 await page.evaluate(()=>document.getElementById('anchor').style.top='630px');
 await openModels();await bounded();
 await page.keyboard.press('Escape');await page.getByRole('menu').waitFor({state:'hidden'});
 await openModels();await page.locator('#outside').click();await page.getByRole('menu').waitFor({state:'hidden'});
 assert.deepEqual(errors,[]);
 console.log('PASS: project entries, bounded long menu, scrolling, selection, resize, Escape, outside click');
}finally{await browser.close();}
