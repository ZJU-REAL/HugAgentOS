import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import { chromium } from 'playwright';
const base=process.env.MIGRATION_BASE_URL || 'http://127.0.0.1:3000';
const out='/tmp/site-migration-e2e';
await fs.mkdir(out,{recursive:true});
const records=[];
async function api(slug,table,method='GET',body){
 const response=await fetch(base+'/site/'+slug+'/__api/data/'+table,{signal:AbortSignal.timeout(15000),method,headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
 const data=await response.json();
 return {status:response.status,data};
}
const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? {executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE} : {})});
const page=await browser.newPage({viewport:{width:1440,height:1050}});
page.setDefaultTimeout(15000);
const errors=[];
page.on('pageerror',error=>errors.push(error.message));
page.on('dialog',dialog=>dialog.dismiss());
try{
 const marker='migration-e2e-'+Date.now();
 const taskSlug='s-9bd2374d';
 const before=await api(taskSlug,'tasks');assert.equal(before.status,200);
 await page.goto(base+'/site/'+taskSlug+'/');
 await page.getByPlaceholder('代办标题').fill(marker);
 const saved=page.waitForResponse(response=>response.url().includes('/__api/data/tasks')&&response.request().method()==='PUT');
 await page.getByRole('button',{name:'添加代办',exact:true}).click();
 assert.equal((await saved).status(),200);
 await page.reload();await page.getByText(marker,{exact:true}).waitFor();
 assert.equal((await api(taskSlug,'tasks')).data.items.find(row=>row.title===marker).done,false);
 const toggled=page.waitForResponse(response=>response.url().includes('/__api/data/tasks')&&response.request().method()==='PUT');
 await page.locator('.todo-card').filter({hasText:marker}).locator('.todo-checkbox').click();
 assert.equal((await toggled).status(),200);
 await page.reload();await page.getByText(marker,{exact:true}).waitFor();
 assert.equal((await api(taskSlug,'tasks')).data.items.find(row=>row.title===marker).done,true);
 await page.screenshot({path:out+'/todo-persisted.png',fullPage:true});
 const deleted=page.waitForResponse(response=>response.url().includes('/__api/data/tasks')&&response.request().method()==='PUT');
 await page.locator('.todo-card').filter({hasText:marker}).locator('.delete-btn').click();
 assert.equal((await deleted).status(),200);
 assert.equal((await api(taskSlug,'tasks')).data.items.some(row=>row.title===marker),false);
 // Stale saves must fail rather than overwrite another visitor's work.
 assert.equal((await api(taskSlug,'tasks','PUT',{revision:before.data.revision,rows:[null]})).status,422);
 assert.equal((await api(taskSlug,'tasks','PUT',{revision:'0'.repeat(64),rows:[]})).status,409);
 assert.equal((await api('s-7068da9e','survey_responses')).status,404);
 const blocked=await api('s-2000c314','survey_responses','POST',{rows:[{}]});assert.notEqual(blocked.status,200);
 const access=JSON.parse(await fs.readFile(process.env.MIGRATION_TEST_ACCESS,'utf8'));
 await page.context().addCookies([access]);
 for(const slug of ['s-7068da9e','s-2000c314']){
  await page.goto(base+'/site/'+slug+'/');
  await page.locator('select').selectOption({index:1});
  await page.locator('.radio-group').nth(0).locator('.radio-item').first().click();
  await page.locator('.radio-group').nth(1).locator('.radio-item').first().click();
  await page.getByRole('button',{name:'下一页'}).click();
  await page.locator('.checkbox-item').first().click();
  await page.getByRole('button',{name:'下一页'}).click();
  await page.locator('span').filter({hasText:/^★$/}).nth(4).click();
  await page.getByRole('button',{name:'下一页'}).click();
  await page.getByPlaceholder('请描述您期望的功能...').fill(marker);
  await page.getByPlaceholder('欢迎畅所欲言...').fill('迁移测试：包含可选字段');
  const submission=page.waitForResponse(response=>response.url().includes('/__api/data/survey_responses')&&response.request().method()==='POST');
  await page.getByRole('button',{name:/提交/}).click();
  const response=await submission;assert.equal(response.status(),200,await response.text());
  records.push({slug,table:'survey_responses',id:(await response.json()).id});
  await page.getByText('感谢您的参与！',{exact:true}).waitFor();
  await page.screenshot({path:out+'/'+slug+'-submitted.png',fullPage:true});
 }
 await page.goto(base+'/site/nebula-demo/');await page.locator('#likeCount').filter({hasText:/^\d+$/}).waitFor();
 const likesBefore=(await api('nebula-demo','likes')).data;
 const originalCount=likesBefore.total;
 const liked=page.waitForResponse(response=>response.url().includes('/__api/data/likes')&&response.request().method()==='POST');
 await page.locator('#likeBtn').click();
 const first=await liked;assert.equal(first.status(),200);
 const likesAfter=(await api('nebula-demo','likes')).data;
 const inserted=likesAfter.items.filter(row=>!likesBefore.items.some(old=>old.id===row.id));
 assert.equal(inserted.length,1);
 records.push({slug:'nebula-demo',table:'likes',id:inserted[0].id});
 console.log('Like button passed');
 console.log('Starting concurrent likes');
 const concurrent=await Promise.all(Array.from({length:8},(_,i)=>api('nebula-demo','likes','POST',{rows:[{delta:1}],request_key:marker+'-like-'+i})));
 for(const response of concurrent){assert.equal(response.status,200);records.push({slug:'nebula-demo',table:'likes',id:response.data.id})}
 await page.reload();await page.locator('#likeCount').filter({hasText:String(originalCount+9)}).waitFor();
 await page.locator('#fbName').fill('迁移验收');
 await page.locator('#fbMsg').fill(marker);
 const feedback=page.waitForResponse(response=>response.url().includes('/__api/data/feedback')&&response.request().method()==='POST');
 await page.locator('#feedbackForm button[type=submit]').click();
 const response=await feedback;assert.equal(response.status(),200);
 records.push({slug:'nebula-demo',table:'feedback',id:(await response.json()).id});
 await page.locator('#fbStatus[data-ok=true]').waitFor();
 await page.screenshot({path:out+'/nebula-persisted.png',fullPage:true});
 assert.equal((await api('nebula-demo','feedback')).status,404);
 assert.deepEqual(errors,[]);
 await fs.writeFile(out+'/result.json',JSON.stringify({passed:true,marker,records,checks:['todo create/reload/toggle/delete','malformed input 422','stale revision 409','two surveys all steps and optional feedback','protected survey retains gate','nine likes including eight concurrent writes','feedback SQL submission','private tables deny public reads','no browser errors']},null,2));
 console.log('Migration browser and HTTP checks passed',records.length);
}finally{
 await fs.writeFile(out+'/test-records.json',JSON.stringify(records));
 await browser.close();
}
