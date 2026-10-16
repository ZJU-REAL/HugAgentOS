import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {resolve,join} from 'node:path';
import {pathToFileURL} from 'node:url';
const consumer=resolve(process.argv[2]||'');
if(!process.argv[2])throw Error('Pass the independent consumer directory from test:consumer');
const module=process.env.WB21_PLAYWRIGHT_MODULE;
if(!module)throw Error('Set WB21_PLAYWRIGHT_MODULE to the existing Playwright index.mjs');
const {chromium}=await import(pathToFileURL(resolve(module)).href);
const child=spawn(process.execPath,[join(consumer,'node_modules/@hugagent/workbench-sdk/dist/tooling/cli.mjs'),'preview',join(consumer,'dist'),'--port','0'],{stdio:['ignore','pipe','pipe']});
let log='',browser;
try {
 const url=await new Promise((ok,fail)=>{
  const timer=setTimeout(()=>fail(Error('preview start timeout')),10000);
  child.once('exit',code=>{clearTimeout(timer);fail(Error('preview exited '+code+' '+log));});
  child.stderr.on('data',data=>{log+=data;});
  child.stdout.on('data',data=>{
   log+=data;const match=log.match(/PREVIEW_SIMULATION (http:\/\/127\.0\.0\.1:\d+)/);
   if(match){clearTimeout(timer);ok(match[1]);}
  });
 });
 browser=await chromium.launch({headless:true});
 const page=await browser.newPage();
 await page.goto(url);
 assert.equal(await page.locator('iframe').count(),0,'Unopened preview must create no iframe');
 await page.getByRole('button',{name:'Open example'}).click();
 await page.getByRole('status').waitFor();
 const frame=page.frameLocator('iframe');
 await frame.locator('#status').filter({hasText:'Ready'}).waitFor();
 await frame.locator('#route').filter({hasText:/^Current view: \/$/}).waitFor();
 await frame.getByRole('button',{name:'Files',exact:true}).click();
 await frame.locator('#route').filter({hasText:'files'}).waitFor();
 await page.goBack();
 await frame.locator('#route').filter({hasText:/^Current view: \/$/}).waitFor();
 await page.locator('#theme').selectOption('dark');
 await frame.locator('body[data-theme=dark]').waitFor();
 await page.locator('#locale').selectOption('zh-CN');
 await frame.locator('html[lang=zh-CN]').waitFor();
 await frame.locator('#query').click();
 await frame.locator('#result').filter({hasText:'Beijing'}).waitFor();
 await frame.locator('#pick').click();
 await frame.locator('#save:not([disabled])').waitFor();
 await frame.locator('#save').click();
 await frame.locator('#result').filter({hasText:'"saved": true'}).waitFor();
 await page.locator('#permissions').selectOption('deny');
 assert.equal(await page.locator('iframe').count(),0);
 await page.locator('#open').click();
 await frame.locator('#status').filter({hasText:'Ready'}).waitFor();
 await frame.locator('#query').click();
 await frame.locator('#status').filter({hasText:'capability_denied'}).waitFor();
 await page.locator('#close').click();
 assert.equal(await page.locator('iframe').count(),0);
 await page.locator('#permissions').selectOption('incompatible');
 await page.locator('#open').click();
 await page.locator('#status').filter({hasText:'host_incompatible'}).waitFor();
 assert.equal(await page.locator('iframe').count(),0);
 assert.equal((await fetch(url+'/missing')).status,404);
 console.log('preview simulation browser passed; real host authorization not tested');
} finally {
 await browser?.close();
 child.kill('SIGTERM');
}
