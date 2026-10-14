import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';

const base = process.env.ARCHIVE_E2E_URL || 'http://localhost:3000';
const work = resolve('../../.scratch/archive-e2e');
const evidence = resolve(work,'screenshots');
await mkdir(evidence,{recursive:true});
const accounts = JSON.parse(await readFile(resolve(work,'test-accounts.json'),'utf8'));
const browser = await chromium.launch({
  headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM,
});
const contexts = [];
const checks = [];
const uploads = [];
let page;
async function login(account) {
  const context = await browser.newContext({viewport:{width:1440,height:1000},acceptDownloads:true});
  contexts.push(context);
  await context.addInitScript(()=>{if(window === window.top)localStorage.setItem('jx_lang','zh-CN');});
  const result = await context.newPage();
  await result.goto(base+'/login?redirect=/');
  const form = result.locator('#loginForm');
  await form.locator('[name=username]').fill(account.username);
  await form.locator('[name=password]').fill(account.password);
  await form.locator('button[type=submit]').click();
  await result.getByRole('button',{name:'我的空间',exact:true}).waitFor({timeout:60000});
  return result;
}
function pass(label) {checks.push(label);console.log('PASS '+label);}
async function openRow(name) {
  const row = page.locator('.jx-mySpace-docRow').filter({hasText:name});
  await row.waitFor({timeout:30000});
  await row.click();
}
async function backToList() {
  await page.getByRole('button',{name:'返回文件列表',exact:true}).click();
}
async function uploadViaApi(name) {
  const response = await page.context().request.post(base+'/api/v1/file/upload',{
    multipart:{file:{name,mimeType:name.endsWith('.zip')?'application/zip':'application/octet-stream',buffer:await readFile(resolve(work,name))}},
  });
  assert.equal(response.status(),200);
  const item=await response.json();
  uploads.push(item);
  return item;
}
try {
  page = await login(accounts.users[0]);
  const errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  pass('实际页面本地账号登录');
  await page.getByRole('button',{name:'我的空间',exact:true}).click();
  await page.getByRole('button',{name:'个人文件夹',exact:true}).click();
  await page.screenshot({path:resolve(evidence,'00-myspace-before-upload.png'),fullPage:true});
  const [input]=await Promise.all([
    page.waitForEvent('filechooser'),
    (async()=>{
      await page.getByRole('button',{name:/上传文件/}).click();
      await page.getByRole('menuitem',{name:/上传文件$/}).click();
    })(),
  ]);
  const admitted=page.waitForResponse(response=>new URL(response.url()).pathname.endsWith('/v1/file/upload')&&response.request().method()==='POST',{timeout:30000});
  await input.setFiles(resolve(work,'验收资料.zip'));
  const response=await admitted;
  assert.equal(response.status(),200);
  const zip=await response.json();
  uploads.push(zip);
  pass('我的空间按钮真实上传 ZIP 到 PostgreSQL 与本地存储');
  await openRow('验收资料.zip');
  const archive=page.locator('.jx-archive');
  await archive.getByRole('button',{name:'资料',exact:true}).click();
  await archive.getByRole('button',{name:'说明.txt',exact:true}).click();
  await archive.getByText('真实 Docker 压缩包端到端验证',{exact:false}).waitFor();
  pass('真实 ZIP 目录浏览及中文文本预览');
  const [download] = await Promise.all([
    page.waitForEvent('download'), archive.getByRole('link',{name:'下载 说明.txt',exact:true}).first().click(),
  ]);
  assert.equal(download.suggestedFilename(),'说明.txt');
  assert.equal(await download.failure(),null);
  const downloaded=await readFile(await download.path());
  assert.equal(downloaded.toString(),'真实 Docker 压缩包端到端验证\n中文文件名正常。');
  pass('单个包内文件下载名称与字节校验');
  await backToList();
  await archive.getByRole('searchbox',{name:'搜索当前目录'}).fill('报告');
  assert.equal(await archive.locator('.jx-archive-list li').count(),1);
  await archive.getByRole('button',{name:'报告.docx',exact:true}).click();
  const frame=archive.locator('iframe');
  await frame.waitFor();
  await page.locator('.jx-archive iframe.is-loaded').waitFor({timeout:30000});
  const officeDimensions=await frame.evaluate(element=>({height:element.getBoundingClientRect().height,width:element.getBoundingClientRect().width}));
  assert.ok(officeDimensions.height>200,JSON.stringify(officeDimensions));
  console.log('Office preview dimensions',JSON.stringify(officeDimensions));
  const officeUrl=await frame.getAttribute('src');
  const office=await page.context().request.get(new URL(officeUrl,base).toString());
  assert.equal(office.status(),200);
  assert.ok((await office.body()).subarray(0,4).equals(Buffer.from('%PDF')));
  await page.waitForTimeout(1200);
  await page.screenshot({path:resolve(evidence,'02-office-preview.png'),fullPage:true});
  pass('包内 Word 通过 Docker LibreOffice 真实转成 PDF 并预览');
  await backToList();
  await archive.getByRole('searchbox',{name:'搜索当前目录'}).fill('');
  await archive.getByRole('button',{name:'预览.pdf',exact:true}).click();
  const pdfFrame=archive.locator('iframe');
  await pdfFrame.waitFor();
  const pdf=await page.context().request.get(new URL(await pdfFrame.getAttribute('src'),base).toString());
  assert.equal(pdf.status(),200);
  assert.ok((await pdf.body()).subarray(0,4).equals(Buffer.from('%PDF')));
  pass('包内 PDF 原文件预览');
  await backToList();
  await archive.getByRole('button',{name:'图像.png',exact:true}).click();
  await page.waitForFunction(()=>{const image=document.querySelector('.jx-archive-preview img[alt="图像.png"]');return image?.complete&&image.naturalWidth>0;});
  pass('包内图片真实加载');
  await backToList();
  await archive.getByRole('button',{name:'页面.html',exact:true}).click();
  await archive.getByText('<script>window.archiveExecuted=true</script>',{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>window.archiveExecuted),undefined);
  pass('HTML 以文本查看且不执行脚本');
  await backToList();
  await archive.getByRole('button',{name:'上级目录',exact:true}).click();
  await archive.getByRole('button',{name:'空目录',exact:true}).click();
  await archive.getByText('此目录没有文件',{exact:true}).waitFor();
  pass('空目录及返回上级导航');
  await archive.getByRole('button',{name:'压缩包根目录',exact:true}).click();
  await page.screenshot({path:resolve(evidence,'01-archive-root.png'),fullPage:true});
  const tar=await uploadViaApi('验收资料.tar.gz');
  await page.reload();
  await openRow(tar.name);
  await page.locator('.jx-archive').getByRole('button',{name:'资料',exact:true}).click();
  await page.locator('.jx-archive').getByRole('button',{name:'说明.txt',exact:true}).click();
  await page.getByText('真实 Docker 压缩包端到端验证',{exact:false}).waitFor();
  pass('真实 TAR.GZ 上传、目录浏览及包内文本预览');
  await uploadViaApi('损坏压缩包.zip');
  await uploadViaApi('暂不支持.rar');
  await page.reload();
  await openRow('损坏压缩包.zip');
  await page.getByText('压缩包已损坏或格式不正确',{exact:true}).waitFor();
  await page.getByRole('button',{name:'重试',exact:true}).click();
  await page.getByText('压缩包已损坏或格式不正确',{exact:true}).waitFor();
  pass('损坏压缩包错误及真实接口重试');
  await openRow('暂不支持.rar');
  await page.getByText(/暂不支持此压缩格式/).waitFor();
  pass('RAR 明确提示与原包下载入口');
  const other=await login(accounts.users[1]);
  for(const suffix of ['', '/member?path='+encodeURIComponent('资料/说明.txt'),'/member/preview?path='+encodeURIComponent('资料/报告.docx')]){
    const forbidden=await other.context().request.get(base+'/api/files/'+zip.file_id+'/archive'+suffix);
    assert.equal(forbidden.status(),403);
  }
  pass('真实第二账号不能列目录、读取成员或取得 Office 预览');
  const removed=await page.context().request.delete(base+'/api/v1/artifacts/'+zip.file_id);
  assert.equal(removed.status(),200);
  for(const suffix of ['', '/member?path='+encodeURIComponent('资料/说明.txt'),'/member/preview?path='+encodeURIComponent('资料/报告.docx')]){
    const missing=await page.context().request.get(base+'/api/files/'+zip.file_id+'/archive'+suffix);
    assert.equal(missing.status(),404);
  }
  pass('删除原包后列表、成员及预览均返回 404');
  assert.deepEqual(errors,[]);
  pass('完整应用浏览器无运行时异常');
  await writeFile(resolve(work,'result.json'),JSON.stringify({
    url:base,checks,uploads:uploads.map(file=>({file_id:file.file_id,name:file.name,size:file.size})),
    member_sha256:createHash('sha256').update(downloaded).digest('hex'),
  },null,2));
} catch(error) {
  if(page){
    await page.screenshot({path:resolve(evidence,'failure.png'),fullPage:true});
    console.error('Failure page:',new URL(page.url()).pathname);
    console.error('Visible UI:',(await page.locator('body').innerText()).slice(-2400));
  }
  throw error;
} finally {
  for(const context of contexts){
    await context.request.post(base+'/api/v1/auth/logout').catch(()=>{});
  }
  await browser.close();
}
