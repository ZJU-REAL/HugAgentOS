import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const output = resolve('node_modules/.tmp/model-reasoning-browser');
await mkdir(output, { recursive: true });
await build({ stdin: { contents: `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { Form, Button } from 'antd';
import ModelEffortChip from './src/components/chat/ModelEffortChip';
import { ReasoningEffortFields } from './src/components/common/ReasoningEffortFields';
import { collectReasoningFields } from './src/utils/reasoningEffort';
import { useChatStore } from './src/stores/chatStore';
import { useModelCapabilitiesStore } from './src/stores/modelCapabilitiesStore';
const vision = { provider_id: 'vision', display_name: 'Vision', model_name: 'vision', provider: 'openai_compatible',
 is_default: false, supports_reasoning_effort: true, reasoning_effort_configured: true,
 reasoning_effort_levels: [{key:'low',value:'low'},{key:'high',value:'high'},{key:'max',value:100}], default_reasoning_effort:'high' };
const main = {...vision, provider_id:'main',display_name:'Main',is_default:true,
 reasoning_effort_levels:[{key:'medium',value:'medium'}], default_reasoning_effort:'medium'};
useChatStore.setState({ chatMode:'medium',modeSlug:'standard' });
useModelCapabilitiesStore.setState({ capabilities:{ ...main,user_model_switch_enabled:true,user_selectable_models:[main,vision] },
 selectedModelProviderId:'main',loaded:true });
function Fixture() {
 const [form] = Form.useForm();
 return <><ModelEffortChip /><Form form={form} initialValues={{
 supports_reasoning_effort:true,provider_type:'chat',model_name:'vision',base_url:'http://model.test',
 reasoning_effort_keys:['low','high','max'],reasoning_effort_values:{low:'low',high:'high',max:'100'},default_reasoning_effort:'high'
 }}>
 <Form.Item name="supports_reasoning_effort" hidden><input /></Form.Item>
 <Form.Item name="provider_type" hidden><input /></Form.Item>
 <Form.Item name="model_name" hidden><input /></Form.Item>
 <Form.Item name="base_url" hidden><input /></Form.Item>
 <ReasoningEffortFields detect={async () => ({levels:[{key:'high',value:'high'},{key:'max',value:100}],
 default:'high',source:'validation_error',notes:[],numeric_range:[1,100]})} />
 <Button onClick={async()=>{ const values=await form.validateFields(); const extra={};collectReasoningFields(values,extra);
 document.querySelector('#saved').textContent=JSON.stringify(extra); }}>Save settings</Button>
 </Form><output id="saved" /></>;
}
createRoot(document.getElementById('root')).render(<Fixture/>);
`, loader:'tsx',resolveDir:process.cwd() },bundle:true,jsx:'automatic',outfile:resolve(output,'fixture.js'),format:'esm',
 define:{'import.meta.env':'{"VITE_DEFAULT_LANGUAGE":"zh-CN"}'},loader:{'.png':'dataurl','.svg':'dataurl'} });
const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
try {
 const page = await browser.newPage({viewport:{width:1000,height:900}});
 const errors = [];
 page.on('pageerror',(error)=>{errors.push(error.message); console.error('PAGE',error.message);});
 await page.route('https://reasoning.test/**', async(route)=>{
   if(route.request().url().endsWith('/fixture.js')) return route.fulfill({contentType:'application/javascript',body:await readFile(resolve(output,'fixture.js'))});
   return route.fulfill({contentType:'text/html',body:'<html><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>'});
 });
 await page.addInitScript(() => localStorage.setItem('jx_lang', 'zh-CN'));
 await page.goto('https://reasoning.test/');
 const trigger=page.getByRole('button',{name:'切换模型与思考强度'});
 await trigger.click();
 await page.locator('.jx-menuCell').first().click();
 await page.getByRole('menuitemradio',{name:'Vision',exact:true}).click();
 assert.match(await trigger.innerText(),/Vision.*思考·高/);
 await trigger.click();
 await page.locator('.jx-menuCell').nth(1).click();
 assert.deepEqual(await page.locator('.jx-menuOption-title').allTextContents(),['快速','思考·低','思考·高','思考·最高']);
 await page.keyboard.press('Escape');
 await page.getByRole('button',{name:'自动探测思考档位'}).click();
 await page.getByText('档位来自上游明确声明。', {exact:false}).waitFor();
 assert.equal(await page.locator('input[id$="reasoning_effort_values_low"]').count(),0);
 assert.equal(await page.locator('input[id$="reasoning_effort_values_max"]').inputValue(),'100');
 assert.equal(await page.locator('#saved').innerText(),'','Discovery must not persist settings');
 await page.getByRole('button',{name:'Save settings'}).click();
 await page.waitForFunction(()=>document.querySelector('#saved').textContent.length>0);
 const saved=JSON.parse(await page.locator('#saved').innerText());
 assert.deepEqual(saved.reasoning_effort_levels,[{key:'high',value:'high'},{key:'max',value:100}]);
 assert.equal(saved.default_reasoning_effort,'high');
 assert.deepEqual(errors,[]);
 await page.screenshot({path:resolve(output,'verified.png'),fullPage:true});
 console.log('Browser: model switching, enabled levels, discovery-only fill, integer mapping save: OK');
} catch(error) { console.error(error); throw error; } finally { await browser.close(); }
