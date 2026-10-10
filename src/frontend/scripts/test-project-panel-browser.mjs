import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const output = resolve('node_modules/.tmp/project-panel-browser');
await mkdir(output, { recursive: true });
await build({
  stdin: { contents: `
import React from 'react';
import { createRoot } from 'react-dom/client';
import ProjectRightRail from './src/components/projects/ProjectRightRail';
import { useProjectStore } from './src/stores/projectStore';
import './src/styles/variables.css';
import './src/styles/projects.css';
import './src/styles/mobile.css';
const files=Array.from({length:2000},(_,i)=>({id:String(i),artifact_id:String(i),name:'docs/file-'+i+'.txt',folder_path:'docs',mime_type:'text/plain',size_bytes:12}));
const project={project_id:'p1',name:'Large project',kind:'team',team_id:'t1',linked_team_folder_id:'r1',permission:'admin',instructions:('Project instruction line.\\n').repeat(80),instructions_revision:'v1'};
useProjectStore.setState({currentProjectId:'p1',currentProject:project,projectFiles:files,
 refreshInstructions:async()=>{},
 updateInstructions:async(text)=>{window.saved=text;useProjectStore.setState({currentProject:{...project,instructions:text}});},
 removeFile:async(id)=>{window.deleted=id;},
});
window.projectStore=useProjectStore;
createRoot(document.getElementById('root')).render(<div style={{width:'min(440px,100%)',margin:'auto'}}><ProjectRightRail/></div>);
`, resolveDir: process.cwd(), loader: 'tsx' },
  outfile: resolve(output, 'fixture.js'), bundle: true, format: 'esm', jsx: 'automatic',
  define: { 'import.meta.env': '{}' }, loader: { '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl' },
  external: ['/loader.gif', '/loader-done.png'],
  plugins: [{
    name: 'preview-boundary',
    setup(build) {
      build.onResolve({filter:/\/file\/FilePreviewPane$/},()=>({path:'preview',namespace:'fixture'}));
      build.onLoad({filter:/.*/,namespace:'fixture'},()=>({contents:'export const FilePreviewPane=({item})=>item?.name || null;',loader:'js'}));
    },
  }],
});
const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE });
try {
 const page = await browser.newPage({viewport:{width:1280,height:1000}});
 await page.addInitScript(() => localStorage.setItem('jx_lang', 'zh-CN'));
 let deletedFolder='';
 const errors=[];page.on('pageerror',error=>errors.push(error.message));
 await page.route('https://project.test/**',async route=>{
   const path=new URL(route.request().url()).pathname;
   if(path==='/fixture.js'||path==='/fixture.css') return route.fulfill({contentType:path.endsWith('.js')?'text/javascript':'text/css',body:await readFile(resolve(output,path.slice(1)))});
   if(path==='/api/v1/teams/t1/folders')return route.fulfill({json:{code:0,data:{tree:[{folder_id:'r1',name:'root',children:[{folder_id:'docs',name:'docs',children:[{folder_id:'sub-correct',name:'sub'}]},{folder_id:'sub-wrong',name:'sub'}]}]}}});
   if(route.request().method()==='DELETE'&&path.includes('/folders/')) deletedFolder=path;
   if(path.startsWith('/api'))return route.fulfill({json:{code:0,data:{count:0,items:[]}}});
   return route.fulfill({contentType:'text/html',body:'<html><head><link rel="stylesheet" href="/fixture.css"><style>*{box-sizing:border-box}body{margin:0;font:14px Arial;background:var(--color-bg-container);color:var(--color-text)}</style></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>'});
 });
 await page.goto('https://project.test/');
 await page.locator('.jx-projectRail-groupHeader').waitFor();
 assert.equal(await page.locator('.jx-projectRail-fileItem').count(),0, 'Folders start collapsed');
 await page.locator('.jx-projectRail-groupHeader').click();
 await page.locator('.jx-projectRail-fileItem').first().waitFor();
 const mounted=await page.locator('.jx-projectRail-fileItem').count();
 assert.ok(mounted>0 && mounted<30, '2000 files should mount fewer than 30 rows: '+mounted);
 const instructions=page.locator('.jx-projectRail-cardText');
 assert.ok((await instructions.boundingBox()).height<=160, 'Instructions leave room for project schedules');
 await page.locator('.jx-projectRail-groupHeader').click();
 await page.waitForTimeout(400);
 assert.equal(await page.locator('.jx-projectRail-fileItem').count(),0);
 await page.locator('.jx-projectRail-groupHeader').click();
 await page.locator('.jx-projectRail-filePreview').first().click();
 await page.getByRole('dialog').waitFor();
 assert.match(await page.getByRole('dialog').innerText(),/file-0.txt/);
 await page.getByRole('button',{name:'Close',exact:true}).click();
 await page.getByRole('dialog').waitFor({state:'hidden'});
 await page.locator('.jx-projectRail-fileItem').first().hover();
 await page.locator('.jx-projectRail-fileItem button[title="删除"]').first().click();
 await page.getByRole('dialog').getByRole('button', {name: /OK|删.*除/}).click();
 assert.equal(await page.evaluate(()=>window.deleted),'0');
 await page.getByRole('dialog',{name:'删除文件？'}).waitFor({state:'hidden'});
 const holder=page.locator('.jx-projectRail-fileTree .ant-tree-list-holder');
 await holder.evaluate(el=>{el.scrollTop=el.scrollHeight;el.dispatchEvent(new Event('scroll'));});
 await page.getByTitle('docs/file-1999.txt',{exact:true}).waitFor();
 assert.ok(await page.locator('.jx-projectRail-fileItem').count()<30);
 await page.screenshot({path:resolve(output,'desktop.png'),fullPage:true});
 // Exercise the real editor and save path.
 await page.evaluate(()=>window.projectStore.getState().setInstructionsEditOpen(true));
 const dialog=page.getByRole('dialog',{name:'编辑项目指令'});
 await dialog.waitFor();
 await page.waitForTimeout(300);
 assert.ok((await dialog.boundingBox()).width>=850);
 const editor=dialog.locator('textarea');
 assert.ok((await editor.boundingBox()).height>=400);
 const counter=dialog.getByText(/\d+ \/ 32768 B/);
 const countBox=await counter.boundingBox();
 const footerBox=await dialog.locator('.ant-modal-footer').boundingBox();
 assert.ok(countBox.y+countBox.height<=footerBox.y, 'Byte count stays above footer');
 await editor.fill('Updated instructions');
 await dialog.getByRole('button',{name:/保.*存/}).click();
 assert.equal(await page.evaluate(()=>window.saved),'Updated instructions');
 await dialog.waitFor({state:'hidden'});
 await page.setViewportSize({width:390,height:844});
 await page.evaluate(()=>window.projectStore.getState().setInstructionsEditOpen(true));
 await page.getByRole('dialog').waitFor();
 await page.waitForTimeout(300);
 assert.ok((await page.getByRole('dialog').boundingBox()).width<=390);
 await page.screenshot({path:resolve(output,'mobile.png'),fullPage:true});
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
 await page.getByRole('button',{name:'Close',exact:true}).click();
 await page.evaluate(()=>window.projectStore.setState({currentProject:{...window.projectStore.getState().currentProject,project_id:'p2'},projectFiles:[
 {id:'root',artifact_id:'root',name:'root.txt',folder_path:'',size_bytes:1},
 {id:'direct',artifact_id:'direct',name:'docs/direct.txt',folder_path:'docs',size_bytes:1},
 {id:'deep',artifact_id:'deep',name:'docs/sub/deep.txt',folder_path:'docs/sub',size_bytes:1},
 ]}));
 await page.getByTitle('root.txt',{exact:true}).first().waitFor();
 assert.equal(await page.locator('.jx-projectRail-fileItem').count(),1);
 assert.equal(await page.getByTitle('sub',{exact:true}).count(),0);
 await page.getByTitle('docs',{exact:true}).first().click();
 await page.getByTitle('sub',{exact:true}).first().waitFor();
 await page.getByTitle('docs/direct.txt',{exact:true}).waitFor();
 assert.equal(await page.getByTitle('docs/sub/deep.txt',{exact:true}).count(),0);
 await page.getByTitle('sub',{exact:true}).first().click();
 await page.getByTitle('docs/sub/deep.txt',{exact:true}).click();
 await page.getByRole('dialog').waitFor();
 assert.match(await page.getByRole('dialog').innerText(),/deep.txt/);
 await page.getByRole('button',{name:'Close',exact:true}).click();
 const subRow=page.locator('.jx-projectRail-groupHeader').filter({has:page.locator('.jx-projectRail-groupName[title="sub"]')});
 await subRow.hover();
 await subRow.getByRole('button',{name:'删除文件夹',exact:true}).click();
 const deletion=page.getByRole('dialog',{name:'删除文件夹「docs/sub」'});
 await deletion.waitFor();
 await deletion.getByRole('button',{name:/删.*除/}).click();
 assert.equal(deletedFolder,'/api/v1/teams/t1/folders/sub-correct', 'Delete resolves nested path, not same-named sibling');
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({files:2000,mountedRows:mounted,checks:'default collapse, nested expansion/deletion, project switch, virtual scroll, preview, delete, counter/footer separation, save, mobile overflow'}));
} catch(error) { const page=browser.contexts()[0]?.pages()[0]; if(page) console.log((await page.locator('body').innerText()).slice(-5000)); throw error; } finally {await browser.close();}
