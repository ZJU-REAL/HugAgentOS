import assert from 'node:assert/strict';
import {readFile,mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {chromium} from 'playwright';
const origin='http://127.0.0.1:13010',out=resolve('../../.scratch/pa-e2e/monitor-ui');await mkdir(out,{recursive:true});
const session=JSON.parse(await readFile(resolve('../../.scratch/pa-e2e/session.json'),'utf8'));
const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
const ctx=await browser.newContext({viewport:{width:1550,height:1050},extraHTTPHeaders:{Authorization:'Bearer pa-e2e-owner'}});
await ctx.addCookies([{name:'jx_session',value:session.token,url:origin,httpOnly:true,sameSite:'Lax'}]);
const errors=[];const page=await ctx.newPage();page.on('pageerror',e=>errors.push(e.message));
async function api(path,body){const r=body?await ctx.request.post(origin+'/api/v1/personal-agent'+path,{data:body}):await ctx.request.get(origin+'/api/v1/personal-agent'+path);assert.ok(r.ok(),await r.text());return (await r.json()).data;}
try {
 await page.goto(origin+'/personal-agent');await page.locator('.pa-panel[aria-busy="true"]').waitFor({state:'hidden'});
 await page.locator('[data-monitor-section="computers"]').waitFor({timeout:45000});
 assert.deepEqual(await page.locator('[data-monitor-section]').evaluateAll(x=>x.map(e=>e.dataset.monitorSection)),['computers','sandbox','tasks','outputs']);
 assert.equal(await page.locator('.pa-panel .jx-toolCallsList').count(),0);
 await page.getByRole('button',{name:'打开并接管',exact:true}).click();
 await page.getByAltText('云电脑屏幕',{exact:true}).waitFor({timeout:60000});
 await page.screenshot({path:resolve(out,'cloud-computer.png'),fullPage:true});
 await page.getByRole('textbox',{name:'云电脑网址'}).fill('http://pa-web-fixture:8000/');
 await page.getByRole('button',{name:'打开网页',exact:true}).click();
 await page.waitForTimeout(2500);
 await page.getByRole('button',{name:'结束接管',exact:true}).click();
 await page.getByAltText('云电脑屏幕',{exact:true}).waitFor({state:'hidden'});
 const nonce=Date.now();
 const admitted=await api('/messages',{content:`监控验收 ${nonce}：在本任务目录创建 monitor-document.txt，内容为 MONITOR_OUTPUT_VERIFIED。创建一张 24x24 PNG 图片 monitor-image.png。可以用 Python 标准库构造 PNG 或用已经安装的库。必须通过导出文件工具把这两个文件保存为下载附件。最后回复两个下载链接。`,intent:'new',request_key:'monitor-'+nonce});
 await page.close();let task;
 for(let i=0;i<160;i++){task=(await api('/work-items')).find(x=>x.goal.includes('监控验收 '+nonce));if(task&&['completed','failed','waiting'].includes(task.status))break;await new Promise(r=>setTimeout(r,2000));}
 assert.equal(task?.status,'completed',task?.waiting_reason||task?.result);
 const thread=await api('/work-items/'+task.id+'/conversation');assert.ok(thread.chat_id.startsWith('pa_chat_task_'));
 assert.ok(thread.messages.some(x=>x.tool_calls?.length));
 const outputs=await api('/outputs');const files=outputs.items.filter(x=>x.work_item_id===task.id||x.task_id===task.id);
 assert.ok(files.some(x=>x.mime_type.startsWith('image/')),JSON.stringify(files));assert.ok(files.some(x=>x.name==='monitor-document.txt'));
 for(const f of files){const r=await ctx.request.get(origin+f.url);assert.equal(r.status(),200);assert.ok((await r.body()).length>0);}
 const reopened=await ctx.newPage();reopened.on('pageerror',e=>errors.push(e.message));await reopened.goto(origin+'/personal-agent');
 const link=reopened.locator('.pa-task-title').filter({hasText:'监控验收 '+nonce});await link.waitFor({timeout:45000});await link.click();
 await reopened.locator('[data-testid="task-conversation"][data-chat-id]').waitFor();assert.equal(await reopened.getByTestId('task-conversation').getAttribute('data-chat-id'),thread.chat_id);
 assert.ok(await reopened.getByTestId('task-conversation').locator('.jx-trs').count()>0);
 await reopened.screenshot({path:resolve(out,'task-conversation.png'),fullPage:true});
 await reopened.locator('.ant-modal-close').click();
 await reopened.locator('[data-monitor-section="outputs"]').getByRole('button',{name:'monitor-document.txt',exact:true}).first().click();
 await reopened.getByText('MONITOR_OUTPUT_VERIFIED',{exact:true}).last().waitFor({timeout:20000});
 await reopened.locator('.ant-modal-close').click();await reopened.screenshot({path:resolve(out,'four-sections.png'),fullPage:true});
 assert.deepEqual(errors,[]);await writeFile(resolve(out,'result.json'),JSON.stringify({passed:true,task_id:task.id,chat_id:thread.chat_id,outputs:files.length,checks:['four sections','cloud desktop takeover','background task after page closed','separate task thread with real tool calls','document and image download','no browser errors']},null,2));
 console.log('PASS: four monitor sections, real cloud screen, background model task, task tool transcript, document/image downloads');
}finally{await browser.close();}
