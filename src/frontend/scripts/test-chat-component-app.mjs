import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
const evidence=resolve('../../.scratch/component-e2e');await mkdir(evidence,{recursive:true});
const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM?{executablePath:process.env.PLAYWRIGHT_CHROMIUM}:{})});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
const errors=[];page.on('pageerror',e=>errors.push(e.message));
try {
 await page.goto('http://localhost:3300/mock-sso/login?auto=0&redirect=/');
 const editor=page.locator('.jx-composerEditor');await editor.waitFor({timeout:60000});
 console.log('PASS 完整应用通过测试身份登录');
 await editor.fill('请简要说明本次界面测试结果。');
 const admitted=page.waitForResponse(r=>r.url().endsWith('/v1/agents/responses'),{timeout:30000});
 await editor.press('Enter');
 const response=await admitted;if(response.status()!==200)throw new Error('Admission '+response.status()+': '+(await response.text()).slice(0,1000));
 await page.getByText('端到端测试已完成。附件内容可以在预览中核对。',{exact:true}).waitFor({timeout:180000});
 await page.locator('.jx-msg.assistant .jx-bubble.streaming').waitFor({state:'hidden'});
 await page.screenshot({path:resolve(evidence,'05-full-app-chat.png'),fullPage:true});
 console.log('PASS 完整聊天链路：浏览器 → nginx → FastAPI → Redis run → 测试模型 → SSE → 回复');
 const input=page.locator('input[type=file]').filter({hasNot:page.locator('[accept]')}).first();
 await input.setInputFiles({name:'e2e-attachment.txt',mimeType:'text/plain',buffer:Buffer.from('真实端到端附件上传测试\n附件内容为本次组件验收使用。')});
 const preview=page.getByRole('button',{name:'预览 e2e-attachment.txt',exact:true});await preview.waitFor({timeout:30000});
 await preview.click();
 await page.getByText('真实端到端附件上传测试',{exact:false}).last().waitFor({timeout:30000});
 await page.screenshot({path:resolve(evidence,'06-full-app-preview.png'),fullPage:true});
 console.log('PASS 真实上传、存储及Canvas文件预览');
 assert.deepEqual(errors,[]);
 console.log('PASS 完整应用浏览器无运行时异常');
} catch(error) {
 await page.screenshot({path:resolve(evidence,'full-app-failure.png'),fullPage:true});
 console.error('URL',page.url(),'UI',(await page.locator('body').innerText()).slice(-2200),'ERRORS',errors);throw error;
} finally {await browser.close();}
