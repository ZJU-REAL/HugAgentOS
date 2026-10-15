import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {readFile,mkdir} from 'node:fs/promises';
import {resolve} from 'node:path';
import {chromium} from 'playwright';
const output=resolve('node_modules/.tmp/workbench-installed-config');
await mkdir(output,{recursive:true});
await build({stdin:{contents:[
 "import {createRoot} from 'react-dom/client';",
 "import {WorkbenchApplicationConfigEditor} from './src/workbench/WorkbenchApplicationConfigEditor';",
 "createRoot(document.getElementById('root')).render(<WorkbenchApplicationConfigEditor token={location.search.includes('session')?'':'config-test'}/>);",
].join(String.fromCharCode(10)),resolveDir:process.cwd(),loader:'tsx'},outfile:resolve(output,'fixture.js'),
 bundle:true,format:'esm',jsx:'automatic',define:{'import.meta.env':JSON.stringify({VITE_API_BASE_URL:"/api",VITE_DEFAULT_LANGUAGE:"zh-CN",VITE_EDITION:"ee"})}});
const browser=await chromium.launch({headless:true});
const key='a'.repeat(64), other='b'.repeat(64);
const fields=[{key:'URL',label:'Provider URL',secret:false,is_set:true,value:'https://provider.test'},
 {key:'TOKEN',label:'Provider credential',secret:true,is_set:true,value:'****'}];
let revision='c'.repeat(64),denied=false,conflict=false,delay=false,deniedStatus=403;
const writes=[], requests=[];
const item=(applicationKey=key)=>({applicationKey,title:applicationKey===key?'Private Reports':'Other Reports',
 ownerUserId:applicationKey===key?'owner-alpha':'owner-beta',origin:{kind:'local',id:'test-node'},revision,
 enabled:true,configured:true,fields});
const page=await browser.newPage();
page.on('pageerror',error=>console.error('fixture page error',error.message));
try {
 await page.addInitScript(()=>localStorage.setItem('jx_admin_token','preserve-admin-token'));
 await page.route('http://127.0.0.1:5268/**',async route=>{
  const request=route.request(),url=new URL(request.url());
  if(url.pathname==='/fixture.js')return route.fulfill({contentType:'text/javascript',body:await readFile(resolve(output,'fixture.js'))});
  if(!url.pathname.startsWith('/api/'))return route.fulfill({contentType:'text/html',body:'<div id="root"></div><script type="module" src="/fixture.js"></script>'});
  requests.push({path:url.pathname,headers:request.headers(),method:request.method()});
  if(denied)return route.fulfill({status:deniedStatus,json:{detail:{code:'permission_denied'}}});
  if(url.pathname.endsWith('/configurable'))return route.fulfill({json:{data:{items:[item(),item(other)]}}});
  assert.ok(url.pathname===('/api/v1/workbench/admin/applications/'+key+'/config')||url.pathname===('/api/v1/workbench/admin/applications/'+other+'/config'));
  const selected=url.pathname.includes(other)?other:key;
  if(request.method()==='GET'){
   assert.equal(url.searchParams.get('revision'),String(revision));
   if(delay&&selected===key)await new Promise(resolve=>setTimeout(resolve,200));
   return route.fulfill({json:{data:item(selected)}}).catch(()=>{});
  }
  const body=request.postDataJSON();writes.push(body);
  if(conflict)return route.fulfill({status:409,json:{detail:{code:'revision_conflict'}}});
  assert.equal(body.revision,revision);
  return route.fulfill({json:{data:item(selected)}});
 });
 await page.goto('http://127.0.0.1:5268/');
 const selector=page.getByRole('combobox',{name:'已安装的工作台应用'});
 await selector.selectOption(key);
 await page.getByLabel('Provider URL',{exact:true}).waitFor();
 assert.equal(await page.getByLabel('Provider URL',{exact:true}).inputValue(),'https://provider.test');
 assert.equal(await page.getByLabel('Provider credential',{exact:true}).inputValue(),'');
 assert.equal(await page.getByLabel('Provider credential',{exact:true}).getAttribute('placeholder'),'已配置，留空则保留不变');
 const labels=await selector.locator('option').allTextContents();
 assert.ok(labels.some(label=>label.includes('Private Reports')&&label.includes('owner-alpha')&&label.includes('本机')));
 assert.ok(!labels.some(label=>label.includes(key)));
 await page.getByLabel('Provider URL',{exact:true}).fill('https://updated.test');
 await page.getByRole('button',{name:'保存应用配置',exact:true}).click();
 await page.getByRole('status').filter({hasText:'工作台应用配置已保存'}).waitFor();
 assert.deepEqual(writes.at(-1),{revision:'c'.repeat(64),values:{URL:'https://updated.test'}});
 await page.getByLabel('Provider credential',{exact:true}).fill('new-provider-secret');
 await page.getByRole('button',{name:'保存应用配置',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('input[type=password]').value==='');
 assert.equal(writes.at(-1).values.TOKEN,'new-provider-secret');
 const clear=page.getByRole('checkbox').nth(1);
 page.once('dialog',dialog=>dialog.dismiss());
 await clear.click();assert.equal(await clear.isChecked(),false);
 page.once('dialog',dialog=>dialog.accept());
 await clear.click();assert.equal(await clear.isChecked(),true);
 await page.getByRole('button',{name:'保存应用配置',exact:true}).click();
 await page.waitForFunction(()=>!document.querySelectorAll('input[type=checkbox]')[1].checked);
 assert.deepEqual(writes.at(-1).clearKeys,['TOKEN']);assert.equal('TOKEN' in writes.at(-1).values,false);
 conflict=true;
 await page.getByLabel('Provider URL',{exact:true}).fill('https://retain-draft.test');
 await page.getByRole('button',{name:'保存应用配置',exact:true}).click();
 await page.getByRole('alert').filter({hasText:'应用配置版本已改变'}).waitFor();
 assert.equal(await page.getByLabel('Provider URL',{exact:true}).inputValue(),'https://retain-draft.test');
 revision='d'.repeat(64);conflict=false;
 await page.getByRole('button',{name:'刷新已安装应用',exact:true}).click();
 await selector.selectOption(key);await page.getByLabel('Provider URL',{exact:true}).waitFor();
 await page.getByRole('button',{name:'保存应用配置',exact:true}).click();
 await page.getByRole('status').filter({hasText:'工作台应用配置已保存'}).waitFor();
 assert.equal(writes.at(-1).revision,'d'.repeat(64));
 delay=true;
 await selector.selectOption(other);await page.getByLabel('Provider URL',{exact:true}).waitFor();
 await selector.selectOption(key);await selector.selectOption(other);
 await page.waitForTimeout(250);
 assert.equal(await page.locator('legend').textContent(),'Other Reports');
 assert.ok(requests.every(request=>request.headers.authorization==='Bearer config-test'));
 assert.ok(requests.every(request=>!JSON.stringify(request).includes('preserve-admin-token')));
 denied=true;
 await page.getByRole('button',{name:'刷新已安装应用',exact:true}).click();
 await page.getByRole('alert').filter({hasText:'请使用具有系统配置权限的账号重新登录'}).waitFor();
 assert.equal(await page.getByLabel('Provider credential',{exact:true}).count(),0);
 assert.equal(await page.evaluate(()=>localStorage.getItem('jx_admin_token')),'preserve-admin-token');
 deniedStatus=401;
 await page.getByRole('button',{name:'刷新已安装应用',exact:true}).click();
 await page.getByRole('alert').filter({hasText:'请使用具有系统配置权限的账号重新登录'}).waitFor();
 denied=false;delay=false;requests.length=0;
 await page.goto('http://127.0.0.1:5268/?session');
 await page.getByRole('combobox',{name:'已安装的工作台应用'}).selectOption(key);
 await page.getByLabel('Provider credential',{exact:true}).waitFor();
 assert.ok(requests.every(request=>request.headers.authorization===undefined));
 assert.equal(await page.evaluate(()=>localStorage.getItem('jx_admin_token')),'preserve-admin-token');
 console.log('public configuration UI: exact install/revision, private owner, masked secrets, explicit clear, conflict, abort and system permission all passed');
}finally{await browser.close();}
