import { createServer } from 'node:http';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {mkdir,readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {chromium} from 'playwright';
const output=resolve('node_modules/.tmp/voice-input-browser');
await mkdir(output,{recursive:true});
await build({entryPoints:['scripts/voice-browser-fixture.tsx'],outfile:resolve(output,'fixture.js'),bundle:true,alias:{'@hugagent/workbench-protocol':resolve('../../packages/workbench-protocol/dist/index.js')},format:'esm',jsx:'automatic',define:{'import.meta.env':'{}'},external:['/loader.gif','/loader-done.png'],loader:{'.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl'},
 plugins:[{name:'worklet-url',setup(b){b.onResolve({filter:/\?url$/},()=>({path:'worklet',namespace:'voice'}));b.onLoad({filter:/.*/,namespace:'voice'},()=>({contents:'export default "/capture.worklet.js"',loader:'js'}));}}]});
// Chromium synthetic device. This fixture never opens a physical microphone.
const server=createServer(async(req,res)=>{
 const files={'/fixture.js':resolve(output,'fixture.js'),'/fixture.css':resolve(output,'fixture.css'),'/capture.worklet.js':resolve('src/components/voice/capture.worklet.js')};
 const file=files[req.url];
 if(!file){res.writeHead(404);res.end();return;}
 res.setHeader('Content-Type',req.url.endsWith('.css')?'text/css':'text/javascript');res.end(await readFile(file));
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const origin=`http://127.0.0.1:${server.address().port}`;
const browser=await chromium.launch({headless:true,args:['--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream',...(process.env.VOICE_TEST_AUDIO_FILE ? [`--use-file-for-fake-audio-capture=${process.env.VOICE_TEST_AUDIO_FILE}`] : [])]});
const page=await browser.newPage({viewport:{width:1100,height:700}});
const errors=[];page.on('pageerror',e=>errors.push(e.message));page.on('console',msg=>{if(msg.type()==='error')console.log('browser:',msg.text());});
const createdModels=[];let voiceGrant;
let liveTranscription=false;
let permitted=true;
let configured=false,fail=false,invalidAudio=false,requests=0,delayed=false,release;
await page.route(`${origin}/**`,async route=>{
 const path=new URL(route.request().url()).pathname;
 if(path==='/fixture.js'||path==='/fixture.css')return route.fulfill({contentType:path.endsWith('.js')?'text/javascript':'text/css',body:await readFile(resolve(output,path.slice(1)))});
 if(path==='/capture.worklet.js')return route.fulfill({contentType:'text/javascript',body:await readFile('src/components/voice/capture.worklet.js')});
 if(path.endsWith('/models/provider-schemas'))return route.fulfill({json:{code:0,data:[
  {id:'openai_compatible',label:'OpenAI 兼容',engine:'openai',supports_types:['chat','embedding','reranker','audio'],fields:[]},
  {id:'audio_compatible',label:'音频转写（OpenAI 兼容）',engine:'openai',supports_types:['audio'],api_key_required:false,fields:[]},
  {id:'anthropic',label:'Anthropic 原生',engine:'native',supports_types:['chat'],fields:[]}]}});
 if(path.endsWith('/models/providers')){
  if(route.request().method()==='POST') {const model=route.request().postDataJSON();createdModels.push(model);return route.fulfill({json:{code:0,data:{...model,provider_id:'test-audio'}}});}
  return route.fulfill({json:{code:0,data:[]}});
 }
 if(path.endsWith('/config/users/owner/voice-permission')){voiceGrant=route.request().postDataJSON().can_use_voice;return route.fulfill({json:{code:0,data:{user_id:'owner',can_use_voice:voiceGrant}}});}
 if(path.endsWith('/config/users'))return route.fulfill({json:{code:0,data:{items:[{user_id:'owner',username:'owner',teams:[],roles:[],status:'active',auth_source:'local',can_use_voice:false,permission_overrides:{},team_permissions:{},role_permissions:{}}]}}});
 if(path.endsWith('/config/teams')||path.endsWith('/config/roles'))return route.fulfill({json:{code:0,data:{items:[]}}});
 if(path.endsWith('/models/roles'))return route.fulfill({json:{code:0,data:[{role_key:'audio_transcription',label:'语音转文字',required_type:'audio',provider_id:null}]}});
 if(path.endsWith('/audio/status'))return route.fulfill({json:{code:0,data:{allowed:permitted,configured:permitted && configured,max_seconds:120}}});
 if(path.endsWith('/audio/transcriptions')){
  requests++;const data=route.request().postDataBuffer();assert.equal(data.toString('ascii',0,4),'RIFF');assert.equal(data.readUInt32LE(24),16000);assert.ok(data.length>4844);assert.equal(route.request().headers()['content-type'],'audio/wav');
  if(liveTranscription){
   const response=await fetch(process.env.VOICE_TEST_API_URL+'/v1/audio/transcriptions',{method:'POST',headers:{'Content-Type':'audio/wav'},body:data});
   return route.fulfill({status:response.status,contentType:'application/json',body:await response.text()});
  }
  if(delayed)await new Promise(resolve=>{release=resolve;});
  return route.fulfill({status:invalidAudio?422:fail?502:200,json:invalidAudio?{detail:'未识别到语音，请重新录音'}:fail?{detail:'音频模型暂不可用'}:{code:0,data:{text:'语音文字',duration_seconds:1}}}).catch(()=>{});
 }
 if(path.startsWith('/api'))return route.fulfill({json:{code:0,data:{mode:'ask',agents:[],skills:[],mcp:[],models:[],main_agent:{},vision:{},user_model_switch:{models:[]}}}});
 return route.fulfill({contentType:'text/html',body:'<html><head><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>'});
});
await page.addInitScript(()=>{
 const original=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
 navigator.mediaDevices.getUserMedia=async constraints=>{
  if(window.denyMic)throw new DOMException('denied','NotAllowedError');
  const stream=await original(constraints);window.voiceTest.streams.push(stream);
  if(window.holdMic)await new Promise(resolve=>{window.releaseMic=resolve;});
  return stream;
 };
});
const mic=()=>page.getByRole('button',{name:'语音输入',exact:true});
const finish=()=>page.getByRole('button',{name:'完成录音',exact:true});
const stopped=async()=>assert.equal(await page.evaluate(()=>window.voiceTest.streams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))),true);
try{
 await page.goto(origin);await mic().waitFor();
 await page.evaluate(()=>window.voiceTest.permission(false));await mic().waitFor({state:'hidden'});
 assert.equal(await page.evaluate(()=>window.voiceTest.streams.length),0);
 await page.evaluate(()=>window.voiceTest.permission(true));await mic().waitFor();
 await mic().click();await page.getByRole('alert').filter({hasText:'尚未配置'}).waitFor();
 assert.equal(await page.evaluate(()=>window.voiceTest.streams.length),0);
 await page.getByRole('button',{name:'关闭',exact:true}).click();configured=true;
 await page.evaluate(()=>{window.voiceTest.set('前文后文');window.voiceTest.attachment();});
 await page.waitForTimeout(100);await page.evaluate(()=>window.voiceTest.cursor(3));
 await mic().click();await finish().waitFor({timeout:12000});await page.waitForTimeout(700);
 await page.screenshot({path:resolve(output,'recording.png')});
 await finish().click();await mic().waitFor();
 assert.equal(await page.evaluate(()=>window.voiceTest.text()),'前文 语音文字 后文');await stopped();
 assert.ok(await page.getByText('kept.txt',{exact:false}).count());
 await page.evaluate(()=>window.voiceTest.undo());await page.waitForTimeout(50);
 assert.equal(await page.evaluate(()=>window.voiceTest.text()),'前文后文');
 // Claude inserts boundary spaces and keeps the cursor before the suffix.
 await page.evaluate(()=>window.voiceTest.set('helloworld'));await page.waitForTimeout(100);await page.evaluate(()=>window.voiceTest.cursor(6));
 await mic().click();await finish().waitFor();await page.waitForTimeout(400);await finish().click();await mic().waitFor();
 assert.equal(await page.evaluate(()=>window.voiceTest.text()),'hello 语音文字 world');
 assert.equal(await page.evaluate(()=>window.voiceTest.selection()),11);
 invalidAudio=true;await mic().click();await finish().waitFor();await page.waitForTimeout(400);await finish().click();await page.getByRole('alert').waitFor();
 const invalidCount=requests;await page.getByRole('button',{name:'重试',exact:true}).click();await finish().waitFor();
 assert.equal(requests,invalidCount);invalidAudio=false;await page.getByRole('button',{name:'取消录音'}).click();await stopped();
 const count=requests;await mic().click();await finish().waitFor();await page.keyboard.press('Escape');await mic().waitFor();await stopped();assert.equal(requests,count);
 await page.evaluate(()=>{window.denyMic=true;});await mic().click();await page.getByRole('alert').filter({hasText:'权限被拒绝'}).waitFor();
 await page.evaluate(()=>{window.denyMic=false;});await page.getByRole('button',{name:'重试',exact:true}).click();await finish().waitFor();await page.waitForTimeout(400);await page.getByRole('button',{name:'取消录音'}).click();await stopped();
 fail=true;await mic().click();await finish().waitFor();await page.waitForTimeout(400);await finish().click();await page.getByRole('alert').waitFor();await stopped();fail=false;
 await page.getByRole('button',{name:'重试',exact:true}).click();await mic().waitFor();assert.match(await page.evaluate(()=>window.voiceTest.text()),/语音文字/);
 delayed=true;await mic().click();await finish().waitFor();await page.waitForTimeout(400);await finish().click();await page.getByRole('status').filter({hasText:'正在转成文字'}).waitFor();
 await page.evaluate(()=>window.voiceTest.set('用户新的输入'));while(!release)await page.waitForTimeout(10);release();release=undefined;delayed=false;
 await page.getByRole('textbox',{name:'转写结果'}).waitFor();assert.equal(await page.evaluate(()=>window.voiceTest.text()),'用户新的输入');
 await page.getByRole('button',{name:'插入文字',exact:true}).click();await mic().waitFor();assert.match(await page.evaluate(()=>window.voiceTest.text()),/语音文字/);
 delayed=true;await mic().click();await finish().waitFor();await page.waitForTimeout(400);await finish().click();while(!release)await page.waitForTimeout(10);
 await page.evaluate(()=>window.voiceTest.switch());release();release=undefined;delayed=false;await mic().waitFor();await stopped();assert.equal(await page.evaluate(()=>window.voiceTest.text()),'');
 await page.evaluate(()=>{window.holdMic=true;});await mic().click();await page.getByRole('button',{name:'取消录音'}).waitFor();
 await page.waitForFunction(()=>!!window.releaseMic);await page.getByRole('button',{name:'取消录音'}).click();await page.evaluate(()=>window.releaseMic());await page.waitForTimeout(100);await stopped();
 assert.equal(await finish().count(),0);
 await page.setViewportSize({width:360,height:740});
 await page.evaluate(()=>{window.holdMic=false;});await mic().click();await finish().waitFor();
 await page.screenshot({path:resolve(output,'mobile-recording.png')});
 const cancelBounds=await page.getByRole('button',{name:'取消录音'}).boundingBox();assert.ok(cancelBounds.x>=0 && cancelBounds.x+cancelBounds.width<=360);
 permitted=false;await finish().waitFor({state:'hidden',timeout:10000});await page.waitForFunction(()=>window.voiceTest.streams.every(s=>s.getTracks().every(t=>t.readyState==='ended')));await stopped();assert.equal(await mic().count(),0);
 permitted=true;await page.evaluate(()=>window.voiceTest.permission(true));
 // Both management surfaces expose audio and recover when switching back to chat.
 await page.setViewportSize({width:1100,height:800});
 for(const path of ['/model-settings','/model-admin']){
  await page.goto(origin+path);await page.getByRole('button',{name:/添加模型/}).click();
  const purpose=page.getByLabel(path==='/model-settings'?'用途':'模型类型',{exact:true});
  await purpose.click();await page.locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden)').getByText('语音模型',{exact:true}).waitFor({state:'attached'});await page.keyboard.press('ArrowUp');await page.keyboard.press('Enter');
  await page.getByText('音频模型用于语音转文字；服务未部署时可留空地址。部署后填写兼容接口的 Base URL，并分配给「语音转文字」角色。',{exact:true}).waitFor();
  await page.getByLabel('厂商 / 协议',{exact:true}).click();
  assert.equal(await page.locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden)').getByText('Anthropic 原生',{exact:true}).count(),0);
  await page.keyboard.press('Escape');await purpose.click();await page.keyboard.press('ArrowDown');await page.keyboard.press('Enter');
  await page.getByLabel('厂商 / 协议',{exact:true}).click();await page.locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden)').getByText('Anthropic 原生',{exact:true}).waitFor();
  assert.equal(await page.locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden)').getByText('音频转写（OpenAI 兼容）',{exact:true}).count(),0);
  await page.keyboard.press('Escape');
  await purpose.click();await page.keyboard.press('ArrowUp');await page.keyboard.press('Enter');
  await page.getByLabel('显示名称',{exact:true}).fill('本机语音模型');
  await page.getByLabel(path==='/model-settings'?'模型名':'模型名称',{exact:true}).fill('Qwen3-ASR-0.6B');
  await page.screenshot({path:resolve(output,path.slice(1)+'.png')});
  await page.getByRole('button',{name:path==='/model-settings'?'OK':/保\s*存/}).click();
  await page.getByRole('dialog').waitFor({state:'hidden'});
  assert.equal(createdModels.at(-1).provider_type,'audio');assert.equal(createdModels.at(-1).base_url,'');
  assert.equal(createdModels.at(-1).provider,'audio_compatible');
 }

 await page.goto(origin+'/user-permissions');
 await page.getByText('配置',{exact:true}).click();
 const voiceRow=page.locator('div[style*="justify-content: space-between"]').filter({has:page.getByText('语音输入',{exact:true})});
 await voiceRow.getByText('开放',{exact:true}).click();
 await page.screenshot({path:resolve(output,'user-voice-permission.png')});
 await page.getByRole('button',{name:/保\s*存/}).click();
 await page.getByRole('dialog').waitFor({state:'hidden'});assert.equal(voiceGrant,true);
 if(process.env.VOICE_TEST_API_URL){
  assert.ok(process.env.VOICE_TEST_AUDIO_FILE,'Live transcription requires a public test audio file');
  await page.goto(origin);configured=true;liveTranscription=true;
  await mic().click();await finish().waitFor();await page.waitForTimeout(4700);await finish().click();
  await page.waitForFunction(()=>window.voiceTest.text().includes('交易'),{},{timeout:70000});
  await stopped();assert.equal(await page.evaluate(()=>window.voiceTest.sent.length),0);
  await page.screenshot({path:resolve(output,'live-asr-result.png')});
  console.log('Live browser → production audio route → local Qwen3-ASR-0.6B → editable composer passed:',await page.evaluate(()=>window.voiceTest.text()));
 }
 assert.deepEqual(errors,[]);
 console.log('Voice browser checks passed: unconfigured, synthetic microphone PCM, cursor/undo/attachments, cancellation, denied/late access, retry, edited draft, navigation, mobile layout, permission grant/revocation, and both speech model forms with deferred URLs.');
}catch(error){console.log('Page state:',(await page.locator('body').innerText()).slice(-2500));console.log('Errors:',errors);await page.screenshot({path:resolve(output,'failure.png')});throw error;}finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
