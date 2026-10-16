import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

const output = resolve('node_modules/.tmp/project-home-parity');
await mkdir(output, { recursive: true });
const shellStubs = {
  name: 'isolate-unrelated-panels',
  setup(builder) {
    builder.onResolve({ filter: /^\.\// }, args => {
      if (!args.importer.endsWith('/components/AppWorkspace.tsx')) return;
      if (args.path === './common/CollapseHeight') return;
      if (args.path === './chat') return { path: 'chat', namespace: 'fixture' };
      if (/^\.\/(sidebar|canvas|common|kb|tool|automation|catalog|docs|lab|myspace|projects|settings|sites)(\/|$)/.test(args.path))
        return { path: args.path, namespace: 'empty-panel' };
    });
    builder.onLoad({ filter: /.*/, namespace: 'fixture' }, () => ({
      contents: `
import {ChatWelcome} from './src/components/chat/ChatWelcome';
import {useChatWelcome} from './src/components/chat/useChatWelcome';
import {useChatStore} from './src/stores/chatStore';
export function ChatArea(props) {
const chat=useChatStore(s=>s.store.chats[s.currentChatId]);
const view=useChatWelcome(chat);
if (chat.messages.length) return <div data-testid="messages">消息</div>;
return <ChatWelcome {...props} view={view} shareAccessLevel="read"/>;
}
export function PromptHubPanel(){return null;}
`, loader: 'tsx', resolveDir: process.cwd(),
    }));
    builder.onLoad({ filter: /.*/, namespace: 'empty-panel' }, () => ({
      contents: `const Empty=()=>null;export default Empty;export {Empty as Sidebar,Empty as SearchModal,Empty as RightSidebarPanel,Empty as AuthExpiredModal,Empty as ImagePreview,Empty as CreateKBModal,Empty as ReindexModal,Empty as ToolResultPanel,Empty as SlidePanel,Empty as ProjectsSkeleton};`,
      loader: 'jsx',
    }));
  },
};
await build({
  stdin: { contents: `
import {createRoot} from 'react-dom/client';
import {useState} from 'react';
import {AppWorkspace} from './src/components/AppWorkspace';
import {useChatStore} from './src/stores/chatStore';
import {usePageConfigStore} from './src/stores/pageConfigStore';
const noop=()=>{};
const chat=(kind)=>({id:'fixture',title:'fixture',createdAt:1,updatedAt:1,messages:[],
...((kind==='project'||kind==='overview')?{projectId:'test-project',projectName:'测试项目'}:{}),
...(kind==='agent'?{agentId:'test-agent',agentName:'测试智能体'}:{}),
...(kind==='site'?{siteChat:true}:{}),
});
const cfg=usePageConfigStore.getState().config;
usePageConfigStore.getState().setConfig({...cfg,branding:{...cfg.branding,hero_title:'共享标题',hero_subtitle:'共享说明'},homepage:{...cfg.homepage,show_logo:true,logo_url:'/brand.svg',show_suggestions:true,suggested_questions:['问题一','问题二','问题三','问题四']}});
useChatStore.setState({currentChatId:'fixture',store:{chats:{fixture:chat('project')},order:[]}});
function Fixture(){
const current=useChatStore(s=>s.store.chats.fixture);
const [visible,setVisible]=useState(true);
const [chatSurface,setChatSurface]=useState(true);
window.fixture={
select:(kind)=>{useChatStore.setState({projectOverviewChatId:kind==='overview'?'fixture':undefined,store:{chats:{fixture:chat(kind)},order:[]}});setVisible(true);},
setSurface:setChatSurface,
setMessages:()=>useChatStore.setState({store:{chats:{fixture:{...current,messages:[{role:'user',content:'测试'}]}},order:[]}}),
config:usePageConfigStore,
};
const state={chat:current,chatSurface,isEmptyChat:!current.messages.length,recommendBarVisible:visible,setRecommendBarVisible:setVisible,recommendBannerText:'共享推荐横幅',handleCapabilityClick:noop,siderCollapsed:true,canvasOpen:false,canvasFullscreen:false,panel:'chat',showChatHeader:false,showHeader:false,inputRef:{current:null},fileInputRef:{current:null},send:noop,detailModal:null,refreshCatalog:noop};
return <AppWorkspace state={state}/>;
}
createRoot(document.getElementById('root')).render(<Fixture/>);
`, resolveDir: process.cwd(), loader: 'tsx' },
  outfile: resolve(output, 'fixture.js'), bundle: true, format: 'esm', jsx: 'automatic',
  define: { 'import.meta.env': '{"VITE_DEFAULT_LANGUAGE":"zh-CN"}' }, loader: { '.css': 'empty' }, plugins:[shellStubs],
});
let css='';
for(const path of ['src/styles/variables.css','src/styles/chat/welcome.css','src/styles/mobile/workspaces.css','src/styles/mobile/chat.css'])css+=await readFile(path,'utf8');
const browser = await chromium.launch({
  headless: true,
  ...(process.env.CHROMIUM_EXECUTABLE ? { executablePath: process.env.CHROMIUM_EXECUTABLE } : {}),
});
try {
const page=await browser.newPage({viewport:{width:1440,height:1000}});
const errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.route('http://parity.test/**',async route=>{
const path=new URL(route.request().url()).pathname;
if(path==='/fixture.js')return route.fulfill({contentType:'text/javascript',body:await readFile(resolve(output,'fixture.js'))});
if(path==='/brand.svg')return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="64" fill="blue"/></svg>'});
return route.fulfill({contentType:'text/html',body:'<html><head><style>'+css+'</style></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>'});
});
await page.goto('http://parity.test/');
await page.locator('.jx-homeBrandMark').waitFor({state:'visible'});
async function snapshot(){
return page.evaluate(()=>({
hero:document.querySelector('.jx-heroBg').innerHTML,
suggestions:document.querySelector('.jx-homeSuggestions').innerHTML,
banner:document.querySelector('.jx-recommendBanner').innerHTML,
main:document.querySelector('.jx-emptyPage').className,
}));
}
assert(await page.locator('.jx-recommendBanner').isVisible());
assert.equal(await page.locator('.jx-heroTitle').textContent(),'共享标题');
assert.equal(await page.locator('.jx-heroSubtitle').textContent(),'共享说明');
assert.equal(await page.locator('.jx-homeSuggestion').count(),3);
assert(await page.locator('.jx-homeBrandMark').evaluate(img=>img.complete&&img.naturalWidth>0));
const projectSnapshot=await snapshot();
await page.evaluate(()=>window.fixture.select('main'));
await page.waitForTimeout(60);
assert.deepEqual(await snapshot(),projectSnapshot,'ordinary and project homepage use identical shared presentation');
await page.evaluate(()=>window.fixture.select('project'));
await page.locator('.jx-homeSuggestionRefresh').click();
assert(await page.getByText('问题四',{exact:true}).isVisible());
await page.locator('.jx-recommendBanner-close').click();
await page.locator('.jx-recommendBanner').waitFor({state:'detached'});
await page.evaluate(()=>window.fixture.select('main'));
assert(await page.locator('.jx-recommendBanner').isVisible());
await page.evaluate(()=>window.fixture.select('project'));
await page.setViewportSize({width:390,height:844});
assert(await page.locator('.jx-mobileHeroText').isVisible());
assert.equal(await page.locator('.jx-emptyPage--main').count(),1);
const mobileProject=await snapshot();
await page.evaluate(()=>window.fixture.select('main'));
await page.waitForTimeout(60);
assert.deepEqual(await snapshot(),mobileProject,'mobile project and main share the same layout');
await page.evaluate(()=>window.fixture.select('overview'));
assert.equal(await page.locator('.jx-heroBg').count(),0,'project overview omits the entire greeting, including mobile text');
assert.equal(await page.locator('.jx-homeSuggestions').count(),0,'project overview omits suggestions and refresh');
assert.equal(await page.locator('.jx-capCards').count(),0);
assert.equal(await page.locator('.jx-homeInput').count(),1,'project overview retains the composer area');
await page.setViewportSize({width:1440,height:1000});
assert.equal(await page.locator('.jx-heroBg').count(),0);
assert.equal(await page.locator('.jx-homeSuggestions').count(),0);
for(const kind of ['agent','site']){
await page.evaluate(kind=>window.fixture.select(kind),kind);
await page.waitForTimeout(60);
assert.equal(await page.locator('.jx-homeBrandMark').count(),0);
assert.equal(await page.locator('.jx-homeSuggestions').count(),0);
assert.equal(await page.locator('.jx-emptyPage--main').count(),0);
}
await page.evaluate(()=>{window.fixture.select('project');window.fixture.setMessages();});
await page.locator('[data-testid="messages"]').waitFor();
await page.locator('.jx-recommendBanner').waitFor({state:'detached'});
await page.evaluate(()=>{window.fixture.select('project');window.fixture.setSurface(false);});
await page.waitForTimeout(300);
assert.equal(await page.locator('.jx-recommendBanner').count(),0);
assert.deepEqual(errors,[]);
console.log('PASS: actual AppWorkspace banner + ChatWelcome; logo, title, subtitle, suggestions and mobile layout match ordinary homepage; project overview hides greeting and suggestions on desktop/mobile while retaining composer; banner close/message/non-chat gating; agent/site semantics preserved; no browser errors.');
}finally{await browser.close();}
