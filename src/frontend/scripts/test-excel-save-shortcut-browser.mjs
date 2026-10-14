import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const output = resolve('node_modules/.tmp/excel-save-shortcut');
await mkdir(output,{recursive:true});
// Covers canvas keyboard-to-file-save wiring using the spreadsheet export adapter.
const adapter = resolve(output,'spreadsheet.tsx');
await writeFile(adapter,`
import {useEffect,useImperativeHandle} from 'react';
export function UniverSpreadsheet({ref,onReadyChange,onDirty}){
 useEffect(()=>{onReadyChange(true)},[onReadyChange]);
 useImperativeHandle(ref,()=>({exportXlsx:async()=>new Blob(['exported-sheet'],{type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}),resetDirty:()=>onDirty(false)}));
 return <button onClick={()=>onDirty(true)}>Modify cell</button>;
}`);
await build({stdin:{contents:`
import React from 'react';
import {createRoot} from 'react-dom/client';
import {CanvasPanel} from './src/components/canvas/CanvasPanel';
import {useCanvasStore} from './src/stores/canvasStore';
window.canvas=useCanvasStore;
useCanvasStore.getState().openCanvas({file_id:'sheet',name:'sheet.xlsx',url:'/files/sheet'});
createRoot(document.getElementById('root')).render(<CanvasPanel/>);
`,resolveDir:process.cwd(),loader:'tsx'},outfile:resolve(output,'fixture.js'),bundle:true,format:'esm',jsx:'automatic',define:{'import.meta.env':'{}'},external:['/loader.gif','/loader-done.png'],loader:{'.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl'},
plugins:[{name:'spreadsheet-adapter',setup(builder){builder.onResolve({filter:/^\.\/UniverSpreadsheet$/},()=>({path:adapter}));}}]});
const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
try{
 const page=await browser.newPage();
 let saves=0,fail=false;
 await page.route('https://sheet.test/**',async route=>{
  const path=new URL(route.request().url()).pathname;
  if(path==='/fixture.js')return route.fulfill({contentType:'text/javascript',body:await readFile(resolve(output,'fixture.js'))});
  if(path==='/api/v1/file/sheet'){
   assert.equal(route.request().method(),'PUT');
   assert.match(route.request().postData(),/exported-sheet/);
   saves++;
   if(fail)return route.fulfill({status:500,json:{detail:'Save failed'}});
   return route.fulfill({json:{file_id:'sheet',name:'sheet.xlsx',size:14,mime_type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',download_url:'/files/sheet'}});
  }
  if(path.startsWith('/api'))return route.fulfill({json:{code:10000,data:[]}});
  return route.fulfill({contentType:'text/html',body:'<html><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>'});
 });
 await page.goto('https://sheet.test/');
 for(const key of ['Control+s','Meta+s']){
  await page.getByRole('button',{name:'Modify cell'}).click();
  await page.waitForFunction(()=>window.canvas.getState().tabs[0].dirty);
  await page.keyboard.press(key);
  await page.waitForFunction(()=>!window.canvas.getState().tabs[0].dirty);
 }
 assert.equal(saves,2,'each shortcut exports and uploads once');
 assert.equal(await page.getByText('保存成功',{exact:true}).count(),0);
 assert.equal(await page.locator('.anticon-check').count(),0);
 fail=true;
 await page.getByRole('button',{name:'Modify cell'}).click();
 await page.keyboard.press('Control+s');
 await page.getByText('Save failed',{exact:true}).waitFor();
 assert.equal(await page.evaluate(()=>window.canvas.getState().tabs[0].dirty),true);
 console.log('Excel canvas: Ctrl+S/Cmd+S export and upload, silent success, visible failure and retained dirty state passed');
}finally{await browser.close();}
