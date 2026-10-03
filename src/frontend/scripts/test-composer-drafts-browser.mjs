
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';
const bundle = await build({ stdin: { contents: `
import React, { useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { InputArea } from './src/components/chat/InputArea';
import { useChatStore } from './src/stores/chatStore';
import { useChatActions } from './src/hooks/useChatActions';
import { readComposer, useComposerStore, chatDraftKey, projectDraftKey } from './src/stores/composerStore';
import { useProjectStore } from './src/stores/projectStore';
import { usePluginStore } from './src/stores/pluginStore';
import { useComposerFiles } from './src/hooks/useComposerFiles';
import { startAutomationCreationInChat } from './src/components/automation/startAutomationCreation';
import { richEditor } from './src/components/chat/composerRichText';
import './src/styles/variables.css';
import './src/styles/chat.css';
useChatStore.setState({ currentUserId: 'draft-owner', currentChatId: 'a',
  store: { order: ['a','b'], chats: Object.fromEntries(['a','b'].map(id => [id,
    {id,title:id,createdAt:1,updatedAt:1,messages:[]}
  ])) } });
usePluginStore.setState({ installed: [{ install_id:'auto-plugin', slug:'automation', description:'定时任务', name:'定时任务插件', enabled:true }], fetchInstalled:async()=>{} });
window.drafts = {
  read: key => readComposer(key), store: useComposerStore,
  homeId: () => useChatStore.getState().homeDraftId,
  markHomeSent: () => {
    const chat = useChatStore.getState();
    chat.addBackendSessionId(chat.homeDraftId);
  },
  createTask: startAutomationCreationInChat,
  openTask: () => useChatStore.getState().adoptChatFromUrl('task-new'),
  transfer: () => {
    const source = readComposer(projectDraftKey('p1'));
    source.consume(source, ['text']);
    useComposerStore.getState().transfer(projectDraftKey('p1'), chatDraftKey('project-chat'));
    useChatStore.getState().adoptChatFromUrl('project-chat');
  },
  document: () => richEditor(document.querySelector('.jx-composerEditor')).getJSON(),
  insert: text => richEditor(document.querySelector('.jx-composerEditor')).commands.insertContent(text),
  inlineChip: () => richEditor(document.querySelector('.jx-composerEditor')).commands.insertContent({
    type:'invocationChip', attrs:{ chip:'plugin', value:JSON.stringify({id:'plugin',name:'插件'}), label:'插件', prefix:'/' }
  }),
};
function Fixture() {
  const inputRef = useRef(null), fileRef = useRef(null);
  const [project, setProject] = useState(null);
  const files = useComposerFiles('/api');
  const actions = useChatActions('/api');
  window.drafts.showChat = () => setProject(null);
  const openProject = id => {
    useProjectStore.setState({ currentProject:{ project_id:id, name:id, permission:'admin' } });
    setProject(id);
  };
  return <main>
    <button onClick={() => { setProject(null); actions.newChat(inputRef); }}>新对话</button>
    <button onClick={() => openProject('p1')}>项目一</button>
    <button onClick={() => openProject('p2')}>项目二</button>
    <button onClick={() => setProject(null)}>返回当前对话</button>
    <button onClick={() => { window.drafts.transfer(); setProject(null); }}>提交项目草稿</button>
    <button onClick={() => useChatStore.getState().setCurrentChatId('a')}>对话 A</button>
    <button onClick={() => useChatStore.getState().setCurrentChatId('b')}>对话 B</button>
    <InputArea projectComposer={!!project} forceSendMode={!!project} inputRef={inputRef} fileInputRef={fileRef} send={() => {}}
      handleFileSelect={files.handleFileSelect} removeFile={files.removeFile} />
  </main>;
}
createRoot(document.getElementById('root')).render(<Fixture />);
`, resolveDir: process.cwd(), loader: 'tsx' }, bundle: true, write: false, format: 'esm', jsx: 'automatic',
outfile: 'fixture.js', define: { 'import.meta.env': '{}' },
loader: { '.css':'css','.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl','.svg':'dataurl' } });
const js = bundle.outputFiles.find(f => f.path.endsWith('.js')).text;
const css = bundle.outputFiles.find(f => f.path.endsWith('.css'))?.text || '';
const browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHROMIUM ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM } : {}) });
try {
 const page = await browser.newPage();
 const errors = [];
 const pendingUploads = [];
 page.on('pageerror', e => { errors.push(e.message); console.error(e.stack); });

 page.setDefaultTimeout(10000);
 await page.addInitScript(() => window.addEventListener('unhandledrejection', e => console.error('REJECTION', e.reason?.stack || e.reason)));

 await page.route('**/*', route => {
   if (route.request().url().endsWith('/v1/file/upload')) { pendingUploads.push(route); return; }
   if (route.request().url().endsWith('/v1/chats') && route.request().method()==='POST') {
     return route.fulfill({json:{code:20000,data:{id:'task-new',title:'新建定时任务',metadata:{automation_task_id:'new'}}}});
   }
   return route.fulfill({json:{code:20000,data:[]}});
 });
 await page.goto('http://drafts.test');
 await page.setContent('<style>' + css + '</style><div id="root"></div>');
 await page.addScriptTag({content: js, type:'module'});
 const editor = page.locator('.jx-composerEditor');
 await editor.waitFor();
 await editor.pressSequentially('A 的未发送草稿');
 await page.getByRole('button', {name:'对话 B',exact:true}).click();
 assert.equal((await editor.innerText()).trim(), '', '切换对话必须显示目标草稿');
 await editor.pressSequentially('B 的草稿');
 await page.getByRole('button', {name:'对话 A',exact:true}).click();
 assert.equal(await editor.innerText(), 'A 的未发送草稿', '返回必须恢复原草稿');

 const click = name => page.getByRole('button',{name,exact:true}).click();
 await click('新对话');
 await editor.pressSequentially('首页草稿');
 await click('对话 B'); await click('新对话');
 assert.equal(await editor.innerText(), '首页草稿', 'the real new-conversation entry restores the unsent home draft');
 await click('新对话');
 assert.equal(await editor.innerText(), '首页草稿', 'repeated entry must not discard the draft');
 await page.evaluate(() => {
   const sent = window.drafts.read();
   window.drafts.insert(' 下一条');
   sent.consume(sent);
 });
 assert.equal(await editor.innerText(), '首页草稿 下一条', 'same-frame edits survive consumption of an older turn');
 await page.evaluate(() => window.drafts.read().setInput('首页草稿'));
 // DOM edits must be flushed to their departing owner even inside IME composition.
 await editor.dispatchEvent('compositionstart');
 await page.evaluate(() => {
   const e = document.querySelector('.jx-composerEditor');
   e.textContent = '正在输入的中文'; e.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertCompositionText',data:'中文',isComposing:true}));
   window.drafts.store.getState();
 });
 await click('对话 B');
 await click('新对话');
 assert.equal(await editor.innerText(), '正在输入的中文');
 // Rich structure and inline chip positions survive navigation exactly.
 await page.evaluate(() => window.drafts.read().setInput('**前文** 后文'));
 await editor.press('Home'); await editor.press('ArrowRight');
 await page.evaluate(() => window.drafts.inlineChip());
 await editor.pressSequentially('插入');
 const before = await page.evaluate(() => window.drafts.document());
 await click('对话 B'); await click('新对话');
 assert.deepEqual(await page.evaluate(() => window.drafts.document()), before);
 await click('对话 A');
 await click('项目一'); await editor.pressSequentially('项目一草稿');
 await page.locator('input[type=file]').first().setInputFiles({name:'project.txt',mimeType:'text/plain',buffer:Buffer.from('project')});
 await page.waitForFunction(() => window.drafts.read().uploadingFiles.size===1);
 await click('项目二'); assert.equal((await editor.innerText()).trim(),'');
 await editor.pressSequentially('项目二草稿');
 await click('返回当前对话');
 assert.equal(await editor.innerText(),'A 的未发送草稿');
 assert.equal(await page.evaluate(() => window.drafts.read().uploadedFiles.length),0);
 await click('项目一');
 assert.equal(await editor.innerText(),'项目一草稿');
 await click('提交项目草稿');
 assert.equal((await editor.innerText()).trim(),'');
 assert.equal(await page.evaluate(() => window.drafts.read().uploadingFiles.size),1);
 assert.equal(pendingUploads.length,1);
 await pendingUploads.shift().fulfill({json:{file_id:'project-file',download_url:'/files/project-file'}});
 await page.waitForFunction(() => window.drafts.read().uploadingFiles.size===0);
 assert.equal(await page.evaluate(() => [...window.drafts.read().uploadedArtifacts.values()][0].file_id),'project-file');
 // The real task creation entry seeds its template once; a deliberately empty draft stays empty.
 await page.evaluate(() => window.drafts.createTask());
 await page.waitForFunction(() => window.drafts.read().input.includes('时间间隔'));
 await editor.press('ControlOrMeta+A'); await editor.press('Backspace');
 await click('对话 B'); assert.equal(await editor.innerText(),'B 的草稿');
 await page.evaluate(() => window.drafts.store.getState().activate('chat:task-new'));
 // Navigate via the conversation store, just as task routes do.
 await page.evaluate(() => window.drafts.openTask());
 assert.equal((await editor.innerText()).trim(),'');
 assert.equal(await page.evaluate(() => window.drafts.read().activePlugin),null);
 await click('新对话');
 const homeBeforeSend = await page.evaluate(() => window.drafts.homeId());
 await page.evaluate(() => window.drafts.markHomeSent());
 await click('对话 B'); await click('新对话');
 assert.equal((await editor.innerText()).trim(), '', 'sent home conversations must not be reopened as drafts');
 assert.notEqual(await page.evaluate(() => window.drafts.homeId()), homeBeforeSend);
 assert.deepEqual(errors,[]);
 console.log('PASS: conversation/home/project/task drafts, rich structure, IME and in-flight project uploads');

} finally { await browser.close(); }
