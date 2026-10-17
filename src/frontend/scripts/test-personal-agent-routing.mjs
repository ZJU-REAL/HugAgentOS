import assert from 'node:assert/strict';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
const origin='http://127.0.0.1:13010';
const output=resolve('../../.scratch/pa-e2e/routing-ui');await mkdir(output,{recursive:true});
const fixture=JSON.parse(await readFile(resolve('../../.scratch/pa-e2e/routing-ui-fixture.json'),'utf8'));
const session=JSON.parse(await readFile(resolve('../../.scratch/pa-e2e/session.json'),'utf8'));
const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
const context=await browser.newContext({viewport:{width:1550,height:1050},extraHTTPHeaders:{Authorization:'Bearer pa-e2e-owner'}});
await context.addCookies([{name:'jx_session',value:session.token,url:origin,httpOnly:true,sameSite:'Lax'}]);
const errors=[];let sent;
try {
  const page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));
  page.on('request',request=>{if(request.url().endsWith('/messages/'+fixture.message_id+'/resolve'))sent=request.postDataJSON();});
  await page.goto(origin+'/personal-agent',{waitUntil:'domcontentloaded'});
  const box=page.locator('.pa-routing-choices').filter({hasText:'请分别填写部署和旅行任务的要求。'});
  await box.waitFor({timeout:45000});
  async function selectOption(input){
    await input.scrollIntoViewIfNeeded();await input.click();await input.press('ArrowUp');await input.press('Enter');await input.press('Escape');
  }
  for(const goal of fixture.goals.slice(0,2)){
    const input=box.getByRole('combobox',{name:'选择关联任务'});await input.click();await input.fill(goal);
    await page.locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option').filter({hasText:goal}).waitFor({state:'visible'});
    await input.press('Enter');
    await box.locator('.ant-select-selection-item').filter({hasText:goal}).waitFor({state:'visible'});
    await input.press('Escape');
  }
  await page.getByText('请分别填写部署和旅行任务的要求。',{exact:true}).click();
  const operations=box.getByRole('combobox',{name:'任务操作'});
  await selectOption(operations.nth(0),'补充任务');await selectOption(operations.nth(1),'补充任务');
  assert.equal(await box.getByRole('button',{name:'确认任务归属'}).isEnabled(),false);
  const fields=box.getByRole('textbox',{name:'此任务的补充要求'});
  await fields.nth(0).fill('部署改用 feature/demo 分支');await fields.nth(1).fill('旅行住 Phra Singh Village');
  await page.screenshot({path:resolve(output,'independent-instructions.png'),fullPage:true});
  await box.getByRole('button',{name:'确认任务归属'}).click();await box.waitFor({state:'hidden'});
  assert.deepEqual(sent.actions,[{task_id:fixture.ids[0],type:'update',text:'部署改用 feature/demo 分支'},{task_id:fixture.ids[1],type:'update',text:'旅行住 Phra Singh Village'}]);
  const response=await context.request.get(origin+'/api/v1/personal-agent/messages');assert.equal(response.status(),200);
  const message=(await response.json()).data.find(row=>row.id===fixture.message_id);
  assert.deepEqual(new Set(message.work_item_ids),new Set(fixture.ids.slice(0,2)));
  assert.equal(message.work_item_id,null);
  await page.waitForFunction(ids => [...document.querySelectorAll('[data-task-ids]')].some(element => ids.every(id => element.getAttribute('data-task-ids').includes(id))), fixture.ids.slice(0,2));assert.deepEqual(errors,[]);
  await writeFile(resolve(output,'result.json'),JSON.stringify({passed:true,checks:['multi-task selection','separate instructions','empty instruction blocks confirmation','real API writes two links','no browser errors']},null,2));
  console.log('PASS: real browser, multi-task clarification, separate instructions, PostgreSQL-backed API');
} catch(error){const page=context.pages()[0];if(page){await page.screenshot({path:resolve(output,'failure.png'),fullPage:true});console.log((await page.locator('body').innerText()).slice(-2500));}throw error;} finally {await browser.close();}
