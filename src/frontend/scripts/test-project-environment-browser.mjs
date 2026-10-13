import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

const output = resolve('../../.scratch/project-environment-201/browser');
await mkdir(output, { recursive: true });
await build({
  stdin: { contents: `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { ConfigProvider, Button } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import ProjectHeader from './src/components/projects/ProjectHeader';
import CreateProjectModal from './src/components/projects/CreateProjectModal';
import { useProjectStore } from './src/stores/projectStore';
import { useAuthStore } from './src/stores/authStore';
import './src/styles/variables.css';
import './src/styles/projects.css';
useAuthStore.setState({authUser:{user_id:'owner',username:'Owner',can_add_mcp:true}});
useProjectStore.setState({
  currentProjectId:'p1', currentProject:{project_id:'p1',name:'行业研究项目',
    kind:'personal',permission:'admin',is_owner:true,description:'围绕产业政策、企业与市场开展研究',favorite:false},
  availableTeams:[],loadTeamTargets:async()=>{},
  createPersonal:async(name,description,folder,environment)=>{
    window.createdEnvironment=environment; return 'created';
  },
});
createRoot(document.getElementById('root')).render(<ConfigProvider locale={zhCN}>
  <main style={{maxWidth:1000,margin:'40px auto',padding:24}}>
    <ProjectHeader projectId="p1" />
    <Button onClick={()=>useProjectStore.getState().setCreateModalOpen(true)}>新建项目</Button>
    <CreateProjectModal />
  </main>
</ConfigProvider>);
`, resolveDir: process.cwd(), loader: 'tsx' },
  outfile: resolve(output, 'fixture.js'), bundle: true, format: 'esm', jsx: 'automatic',
  define: { 'import.meta.env': '{}' },
  loader: { '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl' },
  external: ['/loader.gif', '/loader-done.png'],
});

const options = {
  agent_ids: [{id:'analyst',name:'产业分析师',dependencies:{plugin_ids:['charts']}}],
  plugin_ids: [{id:'charts',name:'数据分析插件',dependencies:{skill_ids:['plot'],mcp_ids:['data']}}],
  skill_ids: [{id:'plot',name:'图表绘制',dependencies:{mcp_ids:['data']}},
              {id:'writing',name:'报告写作',dependencies:{}}],
  mcp_ids: [{id:'data',name:'企业数据连接器',dependencies:{}}],
};
const saved = [];
const errors = [];
const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE });
try {
  const page = await browser.newPage({viewport:{width:1440,height:1100}});
  await page.addInitScript(() => localStorage.setItem('jx_lang','zh-CN'));
  page.on('pageerror',error=>errors.push(error.message));
  let previewFailures = true;
  let config = {mode:'custom',agent_ids:['analyst'],plugin_ids:[],skill_ids:['writing'],mcp_ids:[]};
  await page.route('https://environment.test/**',async route=>{
    const path = new URL(route.request().url()).pathname;
    const respond = data => route.fulfill({json:{code:0,data}});
    if(path==='/fixture.js'||path==='/fixture.css') return route.fulfill({
      contentType:path.endsWith('.js')?'text/javascript':'text/css',
      body:await readFile(resolve(output,path.slice(1))),
    });
    if(path.endsWith('/environment')){
      if(route.request().method()==='PUT'){ config=route.request().postDataJSON(); saved.push(config); }
      return respond({config,revision:'v1',effective:{},dependencies:[],unavailable:[]});
    }
    if(path.endsWith('/project-environments/options')) return respond(options);
    if(path.endsWith('/project-environments/preview')){
      if (previewFailures) return route.fulfill({status:503,json:{detail:'Temporary dependency failure'}});
      const body=route.request().postDataJSON();
      const deps=body.agent_ids?.length ? [
        {kind:'plugin_ids',id:'charts',name:'数据分析插件',required_by:'产业分析师'},
        {kind:'skill_ids',id:'plot',name:'图表绘制',required_by:'数据分析插件'},
        {kind:'mcp_ids',id:'data',name:'企业数据连接器',required_by:'图表绘制'},
      ] : [];
      return respond({effective:{},dependencies:deps,unavailable:[]});
    }
    if(path==='/api/v1/plugins') return respond({items:[{slug:'extra',name:'报告插件',description:'研究报告能力',installed:false,required_secrets:[]}]});
    if(path==='/api/v1/plugins/extra/install'){
      options.plugin_ids.push({id:'extra@owner',name:'报告插件',dependencies:{}});
      return respond({install_id:'extra@owner',name:'报告插件',skills:[],mcp:[]});
    }
    if(path.startsWith('/api')) return respond({tree:[],items:[],categories:[]});
    return route.fulfill({contentType:'text/html',body:'<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/fixture.css"><style>*{box-sizing:border-box}body{margin:0;font:14px Arial;background:var(--color-bg-container);color:var(--color-text)}</style></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>'});
  });
  await page.goto('https://environment.test/');
  await page.getByRole('button',{name:'项目设置',exact:true}).click();
  await page.getByText('绑定环境',{exact:true}).click();
  const environment=page.getByRole('dialog',{name:'绑定项目环境'});
  assert.equal(await environment.getByText('自动加入的依赖',{exact:true}).count(),0);
  await environment.getByRole('button',{name:'保存环境'}).waitFor();
  await environment.getByRole('button',{name:/重\s*试/}).waitFor();
  previewFailures = false;
  await environment.getByRole('button',{name:/重\s*试/}).click();
  await environment.locator('.jx-projectEnvironment-resource').nth(1).getByText('数据分析插件',{exact:true}).waitFor();
  await environment.locator('.jx-projectEnvironment-resource').nth(2).getByText('图表绘制',{exact:true}).waitFor({state:'hidden'});
  await environment.locator('.jx-projectEnvironment-resource').nth(3).getByText('企业数据连接器',{exact:true}).waitFor({state:'hidden'});
  await page.waitForFunction(()=> {
    const button=Array.from(document.querySelectorAll('.ant-modal button'))
      .find(el=>el.textContent.replace(/\s/g,'')==='保存环境');
    return button && !button.disabled;
  });
  // Inferred selections follow their parent, while manually bound skills remain.
  const resources = environment.locator('.jx-projectEnvironment-resource');
  await resources.nth(0).locator('.ant-tag-close-icon').click();
  await resources.nth(1).getByText('数据分析插件',{exact:true}).waitFor({state:'hidden'});
  await resources.nth(2).getByText('图表绘制',{exact:true}).waitFor({state:'hidden'});
  await resources.nth(3).getByText('企业数据连接器',{exact:true}).waitFor({state:'hidden'});
  await resources.nth(2).getByText('报告写作',{exact:true}).waitFor();
  await environment.getByLabel('子智能体',{exact:true}).click();
  await page.getByTitle('产业分析师',{exact:true}).click();
  await environment.getByText('自定义项目环境',{exact:true}).click();
  await resources.nth(1).getByText('数据分析插件',{exact:true}).waitFor();
  await resources.nth(3).getByText('企业数据连接器',{exact:true}).waitFor({state:'hidden'});
  await page.waitForTimeout(300);
  await environment.screenshot({path:resolve(output,'settings-custom.png')});
  // Market installation returns to the same unsaved selection and binds the resource.
  await environment.getByRole('button',{name:'从市场获取'}).nth(1).click();
  const market=page.getByRole('dialog').last();
  await market.getByText('报告插件',{exact:true}).waitFor();
  await market.getByRole('button',{name:/安装/}).click();
  await environment.getByLabel('插件',{exact:true}).waitFor();
  await market.getByRole('button',{name:'Close',exact:true}).click();
  await environment.getByText('报告插件',{exact:true}).waitFor();
  await environment.getByRole('button',{name:'保存环境'}).click();
  await environment.waitFor({state:'hidden'});
  assert.deepEqual(saved[0].agent_ids,['analyst']);
  assert.deepEqual(saved[0].plugin_ids,['extra@owner']);
  assert.deepEqual(saved[0].skill_ids,['writing']);

  await page.getByRole('button',{name:'新建项目',exact:true}).click();
  const creation=page.getByRole('dialog',{name:'创建项目'});
  await creation.getByText('全部可用环境',{exact:true}).waitFor();
  await page.waitForTimeout(300);
  await creation.screenshot({path:resolve(output,'create-default.png'),style:'.ant-message{visibility:hidden}'});
  await creation.getByText('自定义项目环境',{exact:true}).click();
  await creation.getByLabel('子智能体',{exact:true}).waitFor();
  assert.equal(await creation.getByText('暂无额外依赖。未选择资源时，仅保留基础对话能力。').count(),0);
  await creation.getByPlaceholder('给项目起个名字').fill('新研究项目');
  await creation.getByRole('button',{name:'创建项目',exact:true}).click();
  await creation.waitFor({state:'hidden'});
  assert.equal(await page.evaluate(()=>window.createdEnvironment.mode),'custom');
  assert.deepEqual(await page.evaluate(()=>window.createdEnvironment.agent_ids),[]);

  await page.setViewportSize({width:390,height:844});
  await page.getByRole('button',{name:'项目设置',exact:true}).click();
  await page.getByText('绑定环境',{exact:true}).click();
  await page.getByRole('dialog',{name:'绑定项目环境'}).getByText('数据分析插件',{exact:true}).waitFor();
  await page.screenshot({path:resolve(output,'settings-mobile.png'),fullPage:true});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({passed:true,screenshots:output,saved}));
} finally {
  await browser.close();
}
