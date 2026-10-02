// Browser -> production API/session middleware -> isolated SQLite/local storage.
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { writeFile } from 'node:fs/promises';
const base = `http://127.0.0.1:${process.env.CORE_E2E_PORT || '38273'}`;
const output = process.env.CORE_E2E_ROOT || '/tmp/hugagent-core-e2e-20261002';
const title = `Core refactor E2E history ${Date.now()}`;
const browser = await chromium.launch({headless: true, ...(process.env.CHROMIUM_EXECUTABLE ? {executablePath:process.env.CHROMIUM_EXECUTABLE} : {})});
const results = {};
try {
 const context = await browser.newContext();
 const page = await context.newPage();
 const pageErrors=[];
 page.on("pageerror", error => pageErrors.push(error.message));
 await page.goto(base + "/openapi.json");
 const request = async (path, method='GET', body) => page.evaluate(async ({path,method,body}) => {
   const response=await fetch(path,{cache:"no-store",method,headers:body?{'Content-Type':'application/json'}:undefined,body:body?JSON.stringify(body):undefined});
   const text=await response.text(); let data;try{data=JSON.parse(text)}catch{data=text}
   return {status:response.status,data};
 },{path,method,body});
 assert.equal((await request('/api/v1/chats')).status,401);
 await request('/__test/login/core-owner','POST');
 const created=await request('/api/v1/chats','POST',{title});
 assert.equal(created.status,201,JSON.stringify(created));
 const chat=created.data.data.chat_id;
 assert.ok(chat);
 await request(`/__test/history/${chat}`, "POST");
 assert.equal((await request(`/api/v1/chats/${chat}`)).status,200);
 assert.equal((await request(`/api/v1/chats/${chat}/messages`)).status,200);
 const catalog=await request('/api/v1/catalog');
 assert.equal(catalog.status,200,JSON.stringify(catalog));
 assert.ok(catalog.data.data);
 const settings=await request('/api/v1/memories/settings');
 assert.equal(settings.status,200,JSON.stringify(settings));
 const upload=await page.evaluate(async () => {
   const form=new FormData();form.append('file',new Blob(['core refactor file bytes'],{type:'text/plain'}),'core-refactor.txt');
   const r=await fetch('/api/v1/file/upload',{method:'POST',body:form});return {status:r.status,data:await r.json()};
 });
 assert.equal(upload.status,200,JSON.stringify(upload));
 const file=(upload.data.data||upload.data).file_id;
 assert.ok(file,JSON.stringify(upload));
 const downloaded=await request(`/api/files/${file}`);
 assert.equal(downloaded.status,200,JSON.stringify(downloaded));
 assert.equal(downloaded.data,'core refactor file bytes');
 await request('/__test/login/core-other','POST');
 assert.ok([403,404].includes((await request(`/api/v1/chats/${chat}`)).status));
 assert.equal((await request(`/api/v1/artifacts/${file}`,'DELETE')).status,404);
 assert.ok([403,404].includes((await request(`/api/files/${file}`)).status));
 await request('/__test/login/core-owner','POST');
 await page.goto(base);
 await page.getByText(title,{exact:true}).first().waitFor({timeout:30000});
 await page.getByText(title,{exact:true}).first().click();
 await page.getByText('Persisted core refactor history',{exact:true}).first().waitFor({timeout:15000});
 await page.reload();
 await page.getByText('Persisted core refactor history',{exact:true}).first().waitFor({timeout:15000});
 await page.screenshot({path:`${output}/browser.png`,fullPage:true});
 assert.equal((await request(`/api/v1/artifacts/${file}`,'DELETE')).status,200);
 assert.equal((await request(`/api/v1/artifacts/${file}`,'DELETE')).status,404);
 // Soft deletion hides the resource from the list; existing owned links remain readable.
 const listed=await request('/api/v1/artifacts');
 assert.equal(listed.status,200);
 assert.ok(!JSON.stringify(listed.data).includes(file));
 assert.equal((await request(`/api/v1/chats/${chat}`,'DELETE')).status,204);
 assert.equal((await request(`/api/v1/chats/${chat}`)).status,404);
 assert.deepEqual(pageErrors,[],"no browser runtime errors");
 Object.assign(results,{authentication:true,chatCreateHistoryDelete:true,catalog:true,memorySettings:true,fileUploadDownloadDelete:true,crossUserIsolation:true,browserSidebar:true});
 console.log(JSON.stringify(results,null,2));
 await writeFile(`${output}/results.json`,JSON.stringify(results,null,2));
} finally {await browser.close();}
