import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';
import { createServer } from 'node:http';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const evidence = resolve('../../.scratch/component-e2e');
await mkdir(evidence, { recursive: true });
const fixture = `
import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { MessageBubble } from './src/components/chat/MessageBubble';
import { ComposerAttachments } from './src/components/chat/ComposerAttachments';
import { AssetTypeSwitch } from './src/components/myspace/AssetTypeSwitch';
import { ThinkingStepRow } from './src/components/tool/ThinkingStepRow';
import { useComposerFiles } from './src/hooks/useComposerFiles';
import { useComposerStore } from './src/stores/composerStore';
import { useChatStore } from './src/stores/chatStore';
import { useUIStore } from './src/stores/uiStore';
import { processChatStream } from './src/hooks/chatStream';
import { newMessageUid } from './src/utils/messageIdentity';
import './src/styles/variables.css';
import './src/styles/motion.css';
import './src/styles/chat.css';
import './src/styles/tool.css';
useChatStore.setState({currentChatId:'a',currentUserId:'component-test',store:{order:['a','b'],chats:{
 a:{id:'a',title:'test',messages:[],createdAt:1,updatedAt:1},
 b:{id:'b',title:'other',messages:[],createdAt:1,updatedAt:1}
}}});
useUIStore.setState({dispatchProcessVisible:false});
useComposerStore.getState().resetForUser('component-test');
useComposerStore.getState().activate('chat:a');
function Fixture() {
 const [filter,setFilter]=useState('document');
 const [done,setDone]=useState(false);
 const [show,setShow]=useState(true);
 const key=useComposerStore(s=>s.activeKey);
 const files=useComposerFiles('/api');
 const messages=useChatStore(s=>s.store.chats.a.messages);

 const send=async()=>{
  setDone(false);
  useChatStore.getState().updateStore(s=>({...s,chats:{...s.chats,a:{...s.chats.a,messages:[{
   uid:newMessageUid(true),role:'user',content:'分析附件并解释结果',ts:Date.now()
  }]}}}));
  await processChatStream(await fetch('/stream'),{chatId:'a',enableThinking:true});setDone(true);
 };
 return <main style={{maxWidth:780,margin:'24px auto',padding:16}}>
  <h1>聊天组件验收</h1>
  <button onClick={()=>void send()}>开始流式回复</button>
  <button onClick={()=>useChatStore.getState().updateStore(s=>({...s,chats:{...s.chats,a:{...s.chats.a,messages:[{uid:'history',role:'user',content:'历史消息',ts:Date.now()}]}}}))}>加载历史</button>
  <button onClick={()=>document.documentElement.dataset.theme=document.documentElement.dataset.theme==='dark'?'light':'dark'}>切换主题</button>
  <button onClick={()=>{useChatStore.getState().setCurrentChatId('b');useComposerStore.getState().activate('chat:b')}}>草稿 B</button>
  <button onClick={()=>{useChatStore.getState().setCurrentChatId('a');useComposerStore.getState().activate('chat:a')}}>草稿 A</button>
  <button onClick={()=>setShow(value=>!value)}>切换消息视图</button>
  <button onClick={()=>{useComposerStore.getState().resetForUser('other-test-account');useComposerStore.getState().activate('chat:a')}}>切换账号</button>
  <p>{done?'流式结束':'等待回复'}</p>
  <section>{show && messages.map((m,i)=><MessageBubble key={m.uid} m={m} messageIndex={i} currentChatId='a' send={()=>{}} exportChatRecord={async()=>{}} />)}</section>
  <h2>文件类型</h2><AssetTypeSwitch value={filter} onChange={setFilter} />
  <p>{filter==='document'?'显示文档':'显示图片'}</p>
  <h2>附件</h2><input aria-label='选择文件' type='file' multiple onChange={e=>files.handleFileSelect(e,{current:e.target},key)} />
  <ComposerAttachments draftKey={key} currentChatId={key.slice(5)} removeFile={i=>files.removeFile(i,key)} />
  <h2>工具步骤中的思考</h2><ThinkingStepRow content='工具执行前的独立推理内容' active={false}/>
 </main>
}
createRoot(document.getElementById('root')).render(<Fixture/>);
`;
const bundle=await build({stdin:{contents:fixture,resolveDir:process.cwd(),loader:'tsx'},bundle:true,write:false,
 format:'esm',jsx:'automatic',outfile:'fixture.js',external:['/loader.gif','/loader-done.png'],define:{'import.meta.env':'{}'},
 loader:{'.css':'css','.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl','.svg':'dataurl'}});
const js=bundle.outputFiles.find(f=>f.path.endsWith('.js')).text;
const css=(bundle.outputFiles.find(f=>f.path.endsWith('.css'))?.text||'')+' html,body{height:auto;overflow:auto;}';
const uploads=new Map();
const server=createServer(async(req,res)=>{
 const url=new URL(req.url,'http://localhost');
 if(url.pathname==='/stream') {
  res.writeHead(200,{'Content-Type':'text/event-stream'});
  const frame=data=>'data: '+JSON.stringify(data)+'\n\n';
  res.write(frame({type:'thinking',delta:'先检查附件格式。',structured_reasoning:true}));
  setTimeout(()=>res.write(frame({type:'thinking',delta:'然后核对数据和结论。',structured_reasoning:true})),500);
  setTimeout(()=>res.write(frame({type:'content',delta:'**结果**\n\n附件内容已核对。'})),2000);
  setTimeout(()=>res.end('data: [DONE]\n\n'),2500);return;
 }
 if(url.pathname.endsWith('/v1/file/upload')) {
  let body='';for await(const chunk of req)body+=chunk;
  const filename=body.match(/filename="([^"]+)"/)?.[1]||'file';
  const count=(uploads.get(filename)||0)+1;uploads.set(filename,count);
  const reply=()=>{
   res.setHeader('Content-Type','application/json');
   if(filename.includes('retry')&&count===1){res.statusCode=503;res.end(JSON.stringify({detail:'temporary test failure'}));}
   else res.end(JSON.stringify({code:20000,data:{file_id:'test-'+count,download_url:'/files/test-'+count}}));
  };
  setTimeout(reply,filename.includes('slow')?1000:200);return;
 }
 if(url.pathname==='/fixture.js'){res.setHeader('Content-Type','text/javascript');res.end(js);return;}
 if(url.pathname==='/fixture.css'){res.setHeader('Content-Type','text/css');res.end(css);return;}
 if(url.pathname.startsWith('/api/')){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({code:20000,data:[]}));return;}
 if(url.pathname==='/'){res.setHeader('Content-Type','text/html');res.end('<link rel="stylesheet" href="/fixture.css"><div id="root"></div><script type="module" src="/fixture.js"></script>');return;}
 try{const path=resolve('public','.'+url.pathname);if(!path.startsWith(resolve('public')+'/'))throw new Error('invalid path');res.setHeader('Content-Type',path.endsWith('.svg')?'image/svg+xml':path.endsWith('.png')?'image/png':path.endsWith('.gif')?'image/gif':'application/octet-stream');res.end(await readFile(path));}
 catch{res.statusCode=404;res.end();}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM?{executablePath:process.env.PLAYWRIGHT_CHROMIUM}:{})});
const results=[];
const check=(name)=>{results.push(name);console.log('PASS',name)};
try {
 const page=await browser.newPage({viewport:{width:1100,height:1000}});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:'+server.address().port);
 const group=page.getByRole('radiogroup',{name:'类型筛选'});
 await group.getByRole('radio',{name:'文档'}).focus();
 await page.keyboard.press('ArrowRight');
 assert.equal(await group.getByRole('radio',{name:'图片'}).getAttribute('aria-checked'),'true');
 assert.equal(await page.getByText('显示图片',{exact:true}).isVisible(),true);
 await page.keyboard.press('Home');
 assert.equal(await group.getByRole('radio',{name:'文档'}).getAttribute('aria-checked'),'true');
 check('分段筛选方向键、Home、焦点和选中状态');
 await page.getByRole('button',{name:'开始流式回复',exact:true}).click();
 const thinking=page.locator('.jx-msg.assistant .jx-inlineFold').first();
 const summary=thinking.getByRole('button');
 await summary.waitFor();
 await thinking.locator(".jx-inlineSummaryArrow").waitFor();
 assert.equal(await summary.getAttribute('aria-expanded'),'false');
 assert.equal(await thinking.locator('.jx-collapse--open').count(),0);
 await summary.focus();await page.keyboard.press('Enter');
 await thinking.locator('.jx-collapse--open').waitFor();
 await page.getByText('先检查附件格式。然后核对数据和结论。',{exact:true}).waitFor();
 assert.equal(await summary.getAttribute('aria-expanded'),'true');
 await page.screenshot({path:resolve(evidence,'01-stream-thinking.png'),fullPage:true});
 await page.getByText('流式结束',{exact:true}).waitFor();
 assert.equal(await summary.getAttribute('aria-expanded'),'true');
 assert.equal(await summary.innerText(),'思考过程');
 await page.getByText('附件内容已核对。',{exact:true}).waitFor();
 await summary.focus();await page.keyboard.press('Space');
 assert.equal(await thinking.locator('.jx-collapse--open').count(),0);
 check('真实 SSE 解析、思考流式更新、展开保持、完成态及键盘折叠');
 const step=page.locator('.jx-tcr').last();
 await step.getByRole('button').click();
 assert.equal(await step.locator('.jx-thinkingDetailBody').innerText(),'工具执行前的独立推理内容');
 check('工具步骤恢复原有思考展示');
 const pick=page.getByLabel('选择文件');
 await pick.setInputFiles({name:'retry-很长的附件文件名.txt',mimeType:'text/plain',buffer:Buffer.from('test attachment')});
 await page.getByRole('button',{name:/重试上传/}).waitFor();
 await page.screenshot({path:resolve(evidence,'02-upload-error.png'),fullPage:true});
 await page.getByRole('button',{name:/重试上传/}).click();
 await page.getByRole('button',{name:/预览 retry-/}).waitFor();
 assert.equal(uploads.get('retry-很长的附件文件名.txt'),2);
 const card=page.locator('.jx-fileCard').first();
 assert.equal(await card.locator('.jx-fileCard-extension').innerText(),'.txt');
 assert.equal(await card.locator('.jx-fileCard-type').count(),0);
 assert.equal(await card.getByRole('link',{name:/下载/}).count(),1);
 await card.getByRole('button',{name:/移除文件/}).click();
 await card.waitFor({state:'hidden'});
 check('附件失败持久提示、卡片内重试、扩展名、预览下载和移除');
 await pick.setInputFiles({name:'slow-draft.txt',mimeType:'text/plain',buffer:Buffer.from('slow upload')});
 await page.getByRole('button',{name:'草稿 B',exact:true}).click();
 assert.equal(await page.locator('.jx-fileCard').count(),0);
 await page.getByRole('button',{name:'草稿 A',exact:true}).click();
 await page.getByRole('button',{name:'预览 slow-draft.txt',exact:true}).waitFor();
 await page.getByRole('button',{name:'移除文件 slow-draft.txt',exact:true}).click();
 await page.locator('.jx-fileCard').waitFor({state:'hidden'});
 check('上传期间切换草稿，不污染目标草稿');
 await pick.setInputFiles({name:'slow-remove.txt',mimeType:'text/plain',buffer:Buffer.from('remove during upload')});
 await page.getByRole('button',{name:'移除文件 slow-remove.txt',exact:true}).click();
 await page.locator('.jx-fileCard').waitFor({state:'hidden'});
 await page.waitForTimeout(1200);
 assert.equal(await page.locator('.jx-fileCard').count(),0);
 check('移除后的异步上传完成不复活卡片');
 await page.getByRole('button',{name:'加载历史',exact:true}).click();
 assert.equal(await page.locator('.jx-msg--fresh').count(),0);
 check('最近历史消息不重播入场动画');
 await page.getByRole('button',{name:'开始流式回复',exact:true}).click();
 await page.locator('.jx-msg.user.jx-msg--fresh').waitFor();
 await page.getByRole('button',{name:'切换消息视图',exact:true}).click();
 await page.getByRole('button',{name:'切换消息视图',exact:true}).click();
 assert.equal(await page.locator('.jx-msg.user.jx-msg--fresh').count(),0);
 await page.getByText('流式结束',{exact:true}).waitFor();
 check('快速离开再返回，新消息动画不重播');
 await pick.setInputFiles({name:'slow-account.txt',mimeType:'text/plain',buffer:Buffer.from('account boundary')});
 await page.getByRole('button',{name:'切换账号',exact:true}).click();
 await page.waitForTimeout(1200);
 assert.equal(await page.locator('.jx-fileCard').count(),0);
 check('账号切换隔离上传完成');
 await page.emulateMedia({reducedMotion:'reduce'});
 await page.getByRole('button',{name:'切换主题',exact:true}).click();
 await pick.setInputFiles({name:'ready.txt',mimeType:'text/plain',buffer:Buffer.from('ready')});
 await page.getByRole('button',{name:'预览 ready.txt',exact:true}).waitFor();
 await page.screenshot({path:resolve(evidence,'03-dark.png'),fullPage:true});
 await page.setViewportSize({width:390,height:844});
 await page.screenshot({path:resolve(evidence,'04-mobile.png'),fullPage:true});
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
 check('深色、390px窄屏及减少动效');
 assert.deepEqual(errors,[]);check('浏览器无运行时异常');
} finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
console.log(JSON.stringify({passed:results.length,evidence}));
