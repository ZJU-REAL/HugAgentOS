import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';
import { createServer } from 'node:http';
import { mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';

const fixture = `
import React,{useCallback,useState} from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter} from 'react-router';
import {ChatSidebarItem} from './src/components/sidebar/ChatSidebarItem';
import {PersonalAgentSidebarEntry,usePersonalAgentNotifications} from './src/personalAgentEdition';
import {ApplicationButton} from './src/workbench/ApplicationButton';
import {PersonalTaskDialog} from './src/components/personal-agent/tasks/PersonalTaskDialog';
import {useChatResultRead} from './src/hooks/useChatResultRead';
import {usePersonalResultRead} from './src/components/personal-agent/conversation/usePersonalResultRead';
import {useTaskNotificationStore} from './src/stores/taskNotificationStore';
import {useChatStore} from './src/stores/chatStore';
import {useUIStore} from './src/stores/uiStore';
import {useAuthStore} from './src/stores/authStore';
import './src/styles/variables.css';
import './src/styles/sidebar-history.css';
import './src/styles/task-status.css';
const tasks=[['a','分析报告','running'],['b','企业名录','completed'],['c','发送邮件','waiting'],['d','导入数据','failed']];
const chats=tasks.map(([id,title])=>({id,title,createdAt:1,updatedAt:1,messages:[]}));
const projectChats=[...chats.map(c=>({...c,projectId:'project-status'})),{id:'idle',title:'已读会话',createdAt:1,updatedAt:1,messages:[]}];
useAuthStore.setState({authUser:{user_id:'test-owner',can_use_personal_agent:true},authChecking:false});
useChatStore.setState({store:{chats:Object.fromEntries(chats.map(c=>[c.id,c])),order:chats.map(c=>c.id)},sendingChatIds:new Set(['a','c']),remoteRunningChatIds:new Set()});
useTaskNotificationStore.getState().hydrate('browser-test',0);
useTaskNotificationStore.getState().establishPersonalBaseline(0);
useTaskNotificationStore.getState().observe(tasks.map(([id,title,status])=>({key:'chat:'+id,id:'run-'+id,messageId:'msg-'+id,status,startedAt:1,updatedAt:2})));
useUIStore.setState({pendingConfirm:{c:[{confirmId:'confirm-c'}]}});
window.reminders=useTaskNotificationStore;window.chats=useChatStore;window.ui=useUIStore;
function Reader({selected}){
 const [viewport,setViewport]=useState(null);
 const messages=selected==='b'?[{uid:'b',messageId:'msg-b',role:'assistant',content:'result',ts:1}]:selected==='d'?[{uid:'d',messageId:'msg-d',role:'assistant',content:'error',ts:1}]:[];
 useChatResultRead(selected,messages,viewport,false);
 usePersonalResultRead(selected==='personal'?viewport:null,selected==='personal'?[{id:'pa-message',seq:5,role:'assistant',content:'result',work_item_ids:[],task_links:[]}]:[]);
 return <div ref={setViewport} data-testid='reader' style={{height:100,overflow:'auto'}}><div style={{height:400}}>conversation</div><p>{selected}</p></div>;
}
function Fixture(){
 const [dialog,setDialog]=useState(false);
 const [selected,setSelected]=useState('');
 usePersonalAgentNotifications();
 const open=useCallback(id=>setSelected(id),[]);
 const actions={onSelectChat:open,onCommitRename:()=>{},onStartRename:()=>{},onTogglePinned:()=>{},onToggleFavorite:()=>{},onExportChat:()=>{},onDeleteChat:()=>{}};
 return <main><nav id='ordinary'>{chats.map(item=><ChatSidebarItem key={item.id} item={item} groupItems={chats} scopeKey='history' dragScopeRef={{current:null}} actions={actions} active={selected===item.id}/>)}</nav>
 <nav id='project' className='jx-projectGroup'><div className='jx-projectRow'><span className='jx-projectRowIcon'/><span className='jx-projectRowName'>测试项目</span></div><div className='jx-projectChatList'>{projectChats.map(item=><ChatSidebarItem key={item.id} item={item} groupItems={projectChats} scopeKey='project:project-status' dragScopeRef={{current:null}} actions={actions} active={selected===item.id}/>)}</div></nav>
 <nav id='personal'><PersonalAgentSidebarEntry onOpen={()=>open('personal')}/></nav>
 <div id='compact' style={{position:'relative',width:48,height:48}}><ApplicationButton entry={{key:'builtin:personal_agent',title:'Personal Agent',item:{availability:{available:true}}}} onOpen={()=>open('personal')} current={false} className='rail' expanded={false}/></div>
 <Reader selected={selected}/><button id='open-dialog' onClick={()=>{useTaskNotificationStore.getState().observe([{key:'personal:dialogtask',id:'dialogtask:current-run',status:'completed',startedAt:0,updatedAt:2}]);setDialog(true)}}>Open task</button>{dialog&&<PersonalTaskDialog task={{id:'dialogtask',status:'completed',goal:'Result identity test',run_id:'current-run'}} onClose={()=>setDialog(false)} onChange={async()=>{}}/>}</main>;
}
createRoot(document.getElementById('root')).render(<MemoryRouter><Fixture/></MemoryRouter>);
`;
const output=await build({stdin:{contents:fixture,resolveDir:process.cwd(),loader:'tsx'},bundle:true,write:false,
  format:'esm',jsx:'automatic',outfile:'fixture.js',define:{'import.meta.env':'{}'},
  plugins:[{name:'dialog-message-renderer',setup(build){build.onResolve({filter:/\/chat\/MessageBody$/},()=>({path:'MessageBody',namespace:'test-renderer'}));build.onLoad({filter:/.*/,namespace:'test-renderer'},()=>({contents:"import React from 'react';export function MessageBody({m}){return React.createElement('div',null,m.content)}",resolveDir:process.cwd()}));}}],
  loader:{'.css':'css','.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl','.svg':'dataurl'}});
const js=output.outputFiles.find(f=>f.path.endsWith('.js')).text;
const css=output.outputFiles.find(f=>f.path.endsWith('.css')).text;
let personalRunning=true,threadFinal=false;
const server=createServer((req,res)=>{
 if(req.url==='/fixture.js'){res.setHeader('Content-Type','text/javascript');res.end(js);return;}
 if(req.url==='/fixture.css'){res.setHeader('Content-Type','text/css');res.end(css);return;}
 if(req.url.startsWith('/api/')){
   let data={config:{name:'Personal Agent'}};
   if(req.url.includes('/work-items/dialogtask/conversation')){
     const message=(id,seq,content)=>({message_id:id,chat_seq:seq,role:'assistant',content,created_at:'2026-10-14T10:00:00Z',metadata:{},is_streaming:false});
     data={chat_id:'task-thread',task_run_id:threadFinal?'current-run':'old-run',task_status:threadFinal?'completed':'running',terminal_message_id:threadFinal?'current-result':'old-progress',has_more:false,messages:[message('old-progress',1,'Earlier progress'),...(threadFinal?[message('current-result',2,'Final result')]:[])]};
   }
   if(req.url.includes('sidebar-status')){
     const after=Number(new URL(req.url,'http://fixture').searchParams.get('after_seq'));
     data={tasks:[{id:'p',status:personalRunning?'running':'completed',run_id:personalRunning?'first-run':'second-run',updated_at:personalRunning?'2026-10-14T10:00:00Z':'2026-10-14T10:01:00Z',result_seq:personalRunning?null:6}],replying:false,
       latest_seq:personalRunning?5:6,unread_count:[1,3,5,...(personalRunning?[]:[6])].filter(n=>n>after).length};
   }
   res.setHeader('Content-Type','application/json');res.end(JSON.stringify({code:20000,data}));return;
 }
 res.setHeader('Content-Type','text/html');res.end('<link rel="stylesheet" href="/fixture.css"><style>body{margin:20px}main{max-width:360px}nav{padding:12px;background:var(--color-bg-container)}.jx-historyItem{width:300px}.jx-navItem{display:flex;align-items:center;width:300px;gap:8px;border:0;padding:12px;background:var(--color-bg-container);color:var(--color-text)}.rail{position:relative;width:44px;height:44px;border:0;background:var(--color-bg-container)}</style><div id="root"></div><script type="module" src="/fixture.js"></script>');
});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM?{executablePath:process.env.PLAYWRIGHT_CHROMIUM}:{})});
const errors=[];
const evidence=resolve('../../.scratch/sidebar-task-status');await mkdir(evidence,{recursive:true});
try{
 const context=await browser.newContext({viewport:{width:960,height:820}});
 const page=await context.newPage();page.on('pageerror',e=>{errors.push(e.message);console.error(e.stack);});page.on('console',message=>{if(message.type()==='error')console.error('BROWSER',message.text());});
 await page.goto('http://127.0.0.1:'+server.address().port);await page.bringToFront();
 const row=id=>page.locator('#ordinary .jx-historyItem').filter({has:page.locator('[data-task-status="'+id+'"]')});
 await page.locator('#ordinary [data-task-status="running"]').waitFor({timeout:6000}).catch(async error=>{console.error('FIXTURE DOM',await page.locator('#root').innerHTML());console.error('ERRORS',errors);await page.screenshot({path:resolve(evidence,'failure.png'),fullPage:true});throw error;});
 assert.equal(await page.locator('#ordinary [data-task-status="waiting"]').count(),1,'approval outranks running');
 for(const status of ['running','completed','waiting','failed']){
   const dot=page.locator('#ordinary [data-task-status="'+status+'"]');
   assert.equal(await dot.getAttribute('data-task-unread'),'true');
   assert.equal(await dot.evaluate(el=>getComputedStyle(el).animationName),status==='running'?'none':'jx-taskBreathe');
   const ordinaryBounds=await dot.evaluate(el=>({mark:el.getBoundingClientRect().left,title:el.parentElement.querySelector('.jx-historyTitle').getBoundingClientRect().right}));
   assert.ok(ordinaryBounds.mark>=ordinaryBounds.title,'default project status stays to the right');
   const projectDot=page.locator('#project [data-task-status="'+status+'"]');
   const projectBounds=await projectDot.evaluate(el=>({mark:el.getBoundingClientRect().right,title:el.closest('.jx-historyMain').querySelector('.jx-historyTitle').getBoundingClientRect().left}));
   assert.ok(projectBounds.mark<projectBounds.title,'project status uses the left indentation');
 }
 // Backend waiting verdicts can outlive the interactive request after a restart.
 await page.evaluate(()=>window.ui.setState({pendingConfirm:{}}));
 await page.waitForFunction(()=>!document.querySelector('#ordinary [data-task-status="waiting"]'));
 assert.equal(await page.locator('#project [data-task-status="waiting"]').count(),0,'a backend waiting verdict alone has no yellow reminder');
 await page.evaluate(()=>window.ui.setState({pendingUserQuestions:{c:[{requestId:'question-c',questions:[{id:'q',question:'Choose'}]}]}}));
 await page.locator('#ordinary [data-task-status="waiting"]').waitFor();
 await page.evaluate(()=>window.ui.setState({pendingDesignPick:{c:{requestId:'design-c'}},pendingUserQuestions:{}}));
 assert.equal(await page.locator('#ordinary [data-task-status="waiting"]').count(),1,'another actionable interaction keeps the reminder');
 await page.evaluate(()=>window.ui.setState({pendingDesignPick:{}}));
 await page.waitForFunction(()=>!document.querySelector('#ordinary [data-task-status="waiting"]'));
 await page.evaluate(()=>window.ui.setState({pendingConfirm:{c:[{confirmId:'confirm-c'}]}}));
 await page.locator('#ordinary [data-task-status="waiting"]').waitFor();
 const projectTitles=await page.locator('#project .jx-historyTitle').evaluateAll(els=>els.map(el=>el.getBoundingClientRect().left));
 assert.ok(projectTitles.every(left=>Math.abs(left-projectTitles[0])<1),'idle, running and unread project titles remain aligned');
 const spinner=page.locator('#ordinary [data-task-status="running"]');
 assert.equal(await spinner.locator('circle').count(),2,'running shows a track and arc');
 assert.equal(await spinner.locator('.jx-taskSpinnerMotion').evaluate(el=>getComputedStyle(el).animationDuration),'1.5s');
 assert.equal(await page.locator('#ordinary [data-task-status="completed"]').evaluate(el=>getComputedStyle(el).backgroundColor),await page.locator('#ordinary').evaluate(el=>{const probe=document.createElement('span');probe.style.color='var(--color-primary)';el.append(probe);const color=getComputedStyle(probe).color;probe.remove();return color;}),'unread completion uses the primary blue');
 await page.locator('#personal .jx-personalUnread').waitFor();assert.equal(await page.locator('#personal .jx-personalUnread').innerText(),'3');
 assert.equal(await page.locator('#personal .jx-taskStatus').count(),1,'unread messages light the entry');
 assert.equal(await page.locator('#personal [data-task-status]').count(),0,'task progress does not light the entry');
 const compactLabel=await page.locator('#compact button').getAttribute('aria-label');
 assert.match(compactLabel,/3 条未读消息/,'compact navigation announces the unread count');
 assert.doesNotMatch(compactLabel,/运行中/,'compact navigation omits task progress');
 await page.screenshot({path:resolve(evidence,'light-four-states.png'),fullPage:true});
 await row('completed').click();await page.waitForTimeout(100);
 assert.equal(await page.locator('#ordinary [data-task-status="completed"]').getAttribute('data-task-unread'),'true','opening a scrolled-away result does not mark it read');
 await page.getByTestId('reader').evaluate(el=>{el.scrollTop=el.scrollHeight;el.dispatchEvent(new Event('scroll'));});
 await page.waitForFunction(()=>!document.querySelector('#ordinary [data-task-status="completed"]'));
 assert.equal(await page.locator('#ordinary .anticon-check').count(),0,'read completion leaves no check icon');
 await row('failed').click();
 await page.waitForFunction(()=>!document.querySelector('#ordinary [data-task-status="failed"]'));
 assert.equal(await page.evaluate(()=>window.reminders.getState().notices['chat:d'].seen),true,'click acknowledges failure without scrolling');
 await page.evaluate(()=>window.reminders.getState().observe([{key:'chat:d',id:'run-d',messageId:'msg-d',status:'failed',startedAt:1,updatedAt:2}]));
 assert.equal(await page.locator('#ordinary [data-task-status="failed"]').count(),0,'polling does not restore a dismissed failure');
 await page.evaluate(()=>window.reminders.getState().observe([{key:'chat:d',id:'retry-d',messageId:'retry-msg-d',status:'failed',startedAt:3,updatedAt:4}]));
 await page.locator('#ordinary [data-task-status="failed"]').waitFor();
 assert.equal(await page.locator('#ordinary [data-task-status="failed"]').getAttribute('data-task-unread'),'true','a later failed run shows a new reminder');
 await page.locator('#personal button').click();await page.getByTestId('reader').evaluate(el=>{el.scrollTop=el.scrollHeight;el.dispatchEvent(new Event('scroll'));});
 await page.waitForFunction(()=>!document.querySelector('#personal .jx-personalUnread'));
 assert.equal(await page.locator('#personal .jx-taskStatus').count(),0,'reading every message turns the lamp off while a task still runs');
 personalRunning=false;await page.evaluate(()=>window.dispatchEvent(new Event('hugagent:personal-agent-updated')));
 await page.locator('#personal .jx-taskStatus').waitFor();
 assert.equal(await page.locator('#personal .jx-personalUnread').innerText(),'1','completion after reading creates a new unread reminder');
 await page.locator('#open-dialog').click();
 await page.getByText('Earlier progress',{exact:true}).waitFor();
 assert.equal(await page.evaluate(()=>window.reminders.getState().notices['personal:dialogtask'].seen),false,'old progress in a task dialog cannot acknowledge a new completion');
 threadFinal=true;await page.getByText('Final result',{exact:true}).waitFor();
 await page.waitForFunction(()=>window.reminders.getState().notices['personal:dialogtask'].seen);
 await page.locator('.ant-modal-close').click();
 await page.emulateMedia({reducedMotion:'reduce'});
 assert.equal(await spinner.locator('.jx-taskSpinnerMotion').evaluate(el=>getComputedStyle(el).animationName),'jx-taskSpin','reduced motion retains the working indicator');
 for(const status of ['waiting','failed']){
   const motion=await page.locator('#ordinary [data-task-status="'+status+'"]').evaluate(el=>el.getAnimations().map(a=>a.effect.getKeyframes()));
   assert.ok(motion.every(frames=>frames.every(f=>!f.transform||f.transform==='scale(1)')),'reduced motion keeps static state');
 }
 await page.emulateMedia({reducedMotion:'no-preference'});
 await page.evaluate(()=>document.documentElement.dataset.theme='dark');
 await page.screenshot({path:resolve(evidence,'dark-read-and-new-completion.png'),fullPage:true});
 await page.setViewportSize({width:360,height:820});
 assert.ok(await page.locator('#compact .jx-taskStatus').isVisible());
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'narrow preview fits');
 const other=await page.context().newPage();await other.goto('http://127.0.0.1:'+server.address().port);
 await other.locator('#personal .jx-personalUnread').waitFor();
 await page.evaluate(()=>{const store=window.reminders.getState();store.observe([{key:'chat:a',id:'new-a',status:'completed',startedAt:5,updatedAt:6,messageId:'new-message'}]);});
 await other.waitForFunction(()=>window.reminders.getState().notices['chat:a']?.id==='new-a');
 await page.evaluate(()=>window.reminders.getState().markSeen('chat:a','new-a'));
 await other.waitForFunction(()=>window.reminders.getState().notices['chat:a']?.id==='new-a'&&window.reminders.getState().notices['chat:a'].seen);
 await other.close();
 assert.deepEqual(errors,[]);
 console.log('PASS browser: running ring, blue unread dot, left project slots and right default markers; approval priority; actual viewport read; dismissed viewed failure and new failure reminder; Personal Agent lamp only for unread messages; reduced motion; dark mode; narrow rail.');
}finally{await browser.close();await new Promise(r=>server.close(r));}
