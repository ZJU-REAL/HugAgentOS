import assert from 'node:assert/strict';
import { mkdir, writeFile, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { build } from 'esbuild';
import { chromium } from 'playwright';
const output = resolve('node_modules/.tmp/sidebar-navigation');
const origin = 'http://127.0.0.1:5298';
await mkdir(output, { recursive: true });
await build({ stdin: { contents: `
import {createRoot} from 'react-dom/client';
import {createBrowserRouter, RouterProvider} from 'react-router';
import {bindRouter,pathForPanel} from './src/routing/navigation';
import {AppThemeProvider} from './src/AppThemeProvider';
import './src/styles/index';
import {usePanel} from './src/routing/usePanel';
import AppCenter from './src/components/docs/AppCenterPanel';
import {useMySpaceStore} from './src/stores/mySpaceStore';
import {useUIStore} from './src/stores/uiStore';
import {Sidebar} from './src/components/sidebar/Sidebar';
import {useAuthStore} from './src/stores/authStore';
import {useSidebarLayoutStore} from './src/stores/sidebarLayoutStore';
import {useDeploymentModeStore} from './src/stores/deploymentModeStore';
import {reloadDirectory} from './src/workbench/catalog';
import {SidebarLayoutSetting} from './src/components/settings/SidebarLayoutSetting';
import {useApplicationDirectory} from './src/workbench/catalog';
useAuthStore.setState({authUser:{user_id:'wb-reader',allowed_apps:null}});
void useSidebarLayoutStore.getState().load('wb-reader');
window.__sidebarNavigation={layout:layout=>useSidebarLayoutStore.setState({layout}),reload:()=>{reloadDirectory();},
lab:lab_enabled=>useAuthStore.setState({authUser:{...useAuthStore.getState().authUser,lab_enabled}}),
environment:provisionMode=>useDeploymentModeStore.setState({provisionMode}),
collapse:value=>useUIStore.setState({siderCollapsed:value}),theme:value=>useUIStore.getState().setThemeMode(value),open:path=>router.navigate(path)};
useMySpaceStore.setState({notifUnreadCount:7});
const noop=()=>{};
function Fixture(){useApplicationDirectory();const panel=usePanel();return <AppThemeProvider><div className="jx-appShell" style={{height:'100%',display:'flex'}}><Sidebar onNewChat={()=>router.navigate('/')}
onNewProjectChat={noop} onDeleteChat={noop} onTogglePinned={noop} onToggleFavorite={noop}
onStartRename={noop} onCommitRename={noop} onExportChat={noop} onSelectChat={noop} onSetPanel={(p,s)=>router.navigate(pathForPanel(p,s))}/>
<main style={{flex:1,overflow:'auto'}}>{panel==='app_center'?<AppCenter/>:panel==='settings'?<SidebarLayoutSetting/>:<h1>{panel}</h1>}</main></div></AppThemeProvider>}
const router=createBrowserRouter([{path:'*',element:<Fixture/>}]);
bindRouter(router);createRoot(document.getElementById('root')).render(<RouterProvider router={router}/>);
`, resolveDir: process.cwd(), loader: 'tsx' }, outfile: resolve(output, 'fixture.js'), bundle: true,
external: ['/loader.gif','/loader-done.png'], format: 'esm', jsx: 'automatic', define: { 'import.meta.env': '{}' },
loader: { '.css': 'css', '.svg': 'dataurl', '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl' } });
await writeFile(resolve(output, 'index.html'), '<html><head><style>#root{height:100%}</style><link rel="stylesheet" href="/fixture.css"></head><body style="margin:0;height:100vh"><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>');
const applications = Array.from({length: 100}, (_, n) => ({
  applicationKey: 'external:report_' + n, revision: 1, enabled: true,
  declaration: {schemaVersion: '1.0.0', id: 'report_' + n, title: 'Report ' + n,
    description: 'Analysis ' + n, target: {kind: 'external', mode: 'browser', url: origin + '/destination/' + n}},
  availability: {available: n !== 0, reason: n === 0 ? 'app_disabled' : null},
  navigation: {defaultPinned: true, forced: true},
}));
const pluginOrigin = {kind: 'cloud', id: 'a'.repeat(32)};
const pluginKey = 'plugin:cloud:' + pluginOrigin.id + ':' + 'b'.repeat(64) + ':20261018T000000.000000Z:report';
const plugin = {applicationKey: pluginKey, revision: 1, enabled: true, origin: pluginOrigin,
 declaration: {schemaVersion: '1.0.0', id: 'report', title: 'Plugin reports', target: {kind: 'plugin'}},
 binding: {origin: pluginOrigin, executionScope: 'cloud'}, availability: {available: true, reason: null},
 navigation: {defaultPinned: true, forced: true}};
const builtins = ['chat','projects','my_space','ability_center','app_center','automation','sites','lab','settings','docs'].map(id => ({
  applicationKey: 'builtin:' + id, revision: 1, enabled: true,
  declaration: {schemaVersion: '1.0.0', id, title: id, target: {kind: 'builtin', componentId: id}},
  availability: {available: true, reason: null}, navigation: {defaultPinned: false, forced: false},
}));
const browser = await chromium.launch({headless: true});
const errors = [], requests = [];
let total = 100;
let savedLayout = 'modern';
let failDirectory = false;
try {
 const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
 page.on('pageerror', error => errors.push(error.message));
 await page.addInitScript(() => localStorage.setItem('jx_lang', 'zh-CN'));
 await page.context().route(origin + '/**', async route => {
  const path = new URL(route.request().url()).pathname;
  if (['/fixture.js','/fixture.css'].includes(path)) return route.fulfill({contentType: path.endsWith('.css') ? 'text/css' : 'text/javascript', body: await readFile(resolve(output, path.slice(1)))});
  if (path.startsWith('/home/')) return route.fulfill({contentType: 'image/svg+xml', body: await readFile(resolve('public', path.slice(1))).catch(() => '<svg xmlns="http://www.w3.org/2000/svg"/>')});
  if (!path.startsWith('/api/')) return route.fulfill({contentType: 'text/html', body: path.startsWith('/destination/') ? '<h1>Report destination</h1>' : await readFile(resolve(output, 'index.html'))});
  requests.push(path);
  let data = [];
  if (path.endsWith('/workbench/catalog') && failDirectory) return route.fulfill({status: 503, body: '{}'});
  if (path.endsWith('/users/wb-reader/preferences')) {
   if (route.request().method() === 'PUT') savedLayout = route.request().postDataJSON().sidebar_layout;
   data = {sidebar_layout: savedLayout};
  }
  if (path.endsWith('/workbench/catalog')) data = {origin: {kind: 'cloud', id: 'a'.repeat(32)}, revision: String(total), items: [...builtins, ...applications.slice(0, total), ...(total ? [plugin] : [])], policy: {revision: 7, defaultPinned: applications.map(i => i.applicationKey), forced: applications.map(i => i.applicationKey)}};
  if (path.endsWith('/catalog/events')) return route.fulfill({status: 503, body: '{}'});
  if (path.endsWith('/me/navigation-preferences')) data = {revision: 9, environment: 'web', pinned: applications.map(i => i.applicationKey), order: applications.map(i => i.applicationKey).reverse()};
  if (path.endsWith('/launch')) {const key = decodeURIComponent(path.split('/').at(-2)); data = {applicationKey: key, target: applications.find(i => i.applicationKey === key).declaration.target};}
  return route.fulfill({json: {code: 10000, data}});
 });
 const expected = ['项目','站点','应用中心','实验室'];
 async function more(names = expected) {
  const trigger = page.getByRole('button', {name: '更多', exact: true});
  await trigger.click();
  const menu = page.getByRole('menu');
  await menu.waitFor();
  assert.deepEqual(await menu.getByRole('menuitem').allTextContents(), names, 'More contains module entries only, in product order');
  assert.equal(await menu.getByText(/Report|Plugin reports/).count(), 0);
  return menu;
 }
 await page.goto(origin);
 console.log('checking More and secondary navigation');
 await page.getByRole('button', {name: '用户菜单', exact: true}).waitFor();
 await more();
 await page.keyboard.press('Escape');
 await page.waitForFunction(() => document.activeElement?.getAttribute('aria-label') === '更多');
 for (const [name, path] of [['项目','/projects'],['站点','/sites'],['应用中心','/app-center'],['实验室','/lab']]) {
  const menu = await more();
  await menu.getByRole('menuitem', {name, exact: true}).click();
  await page.waitForURL('**' + path);
  const secondary = page.locator('.jx-moduleSidebar');
  await secondary.locator('button[aria-current=page]').filter({hasText: name}).waitFor();
  assert.deepEqual(await secondary.locator('.jx-moduleCategories button').allTextContents(), expected, name + ' restores secondary module navigation');
  assert.equal(await secondary.getByRole('button', {name, exact: true}).getAttribute('aria-current'), 'page');
  await page.reload();
  await secondary.locator('button[aria-current=page]').filter({hasText: name}).waitFor();
 }
 await page.locator('.jx-moduleSidebar').getByRole('button', {name: '应用中心', exact: true}).click();
 console.log('checking app center');
 const center = page.locator('.jx-agentPage');
 const report = center.locator('[data-application-open="external:report_99"]');
 await report.waitFor();
 assert.equal(await center.getByRole('searchbox').count(), 0);
 assert.ok(await center.locator('.jx-agentPage-grid .jx-agentCard').count() >= 100);
 assert.equal(await center.getByRole('checkbox').count(), 0, 'app center cannot pin individual apps into sidebar');
 const popupPromise = page.waitForEvent('popup');
 await report.focus();
 await page.keyboard.press('Enter');
 const popup = await popupPromise;
 await popup.waitForURL('**/destination/99');
 await popup.close();
 assert.equal(await center.locator('[data-application-open="external:report_0"]').isDisabled(), true, 'disabled app remains disabled');
 await center.locator('[data-application-open="' + pluginKey + '"]').waitFor();
 await page.getByRole('button', {name: '收起侧边栏', exact: true}).click();
 const menu = await more();
 await menu.getByRole('menuitem', {name: '项目', exact: true}).click();
 await page.locator('.jx-moduleSidebar').waitFor();
 console.log('checking layouts and catalogs');
 for (const count of [0, 5, 100]) {
  total = count;
  await page.evaluate(() => window.__sidebarNavigation.reload());
  for (const layout of ['modern','classic']) {
   await page.evaluate(layout => {window.__sidebarNavigation.layout(layout); window.__sidebarNavigation.collapse(false);}, layout);
   await more(); await page.keyboard.press('Escape');
   const nav = page.locator(layout === 'modern' ? '.jx-moduleRail' : '.jx-classicNav');
   assert.equal(await nav.getByText(/Report|Plugin reports/).count(), 0, 'individual applications never enter either sidebar');
   await page.getByRole('button', {name: '用户菜单', exact: true}).click();
   assert.equal(await page.getByRole('menu').getByText(/Report|Plugin reports/).count(), 0, 'user menu contains no individual applications');
   await page.keyboard.press('Escape');
  }
 }
 await page.evaluate(() => {window.__sidebarNavigation.layout('modern'); window.__sidebarNavigation.lab(false);});
 await more(['项目','应用中心']); await page.keyboard.press('Escape');
 await page.evaluate(() => {window.__sidebarNavigation.lab(true); window.__sidebarNavigation.environment('local_only');});
 await page.getByRole('button', {name: '能力中心', exact: true}).waitFor();
 assert.equal(await page.locator('.jx-moduleRail').getByRole('button', {name: '我的空间', exact: true}).count(), 0);
 await more(); await page.keyboard.press('Escape');
 await page.evaluate(() => {window.__sidebarNavigation.environment('cloud_only'); window.__sidebarNavigation.open('/settings');});
 await page.getByRole('radio', {name: /经典模式/}).waitFor();
 const layoutApply = page.locator('.jx-layoutActions').getByRole('button');
 assert.equal(await layoutApply.isDisabled(), true, 'unchanged layout cannot be applied');
 await page.getByRole('radio', {name: /经典模式/}).check();
 await layoutApply.click();
 await page.getByRole('dialog').getByRole('button', {name: /取\s*消/}).click();
 assert.equal(savedLayout, 'modern', 'cancel does not persist draft');
 await layoutApply.click();
 const reload = page.waitForEvent('load');
 await page.getByRole('dialog').getByRole('button', {name: '应用并刷新', exact: true}).click();
 await reload;
 await page.locator('.jx-classicShell').waitFor();
 assert.equal(savedLayout, 'classic', 'confirmation saves layout before reload');
 await page.getByRole('button', {name: '更多', exact: true}).click();
 await page.getByRole('menuitem', {name: '项目', exact: true}).click();
 const classicSecondary = page.locator('.jx-classicModuleShell');
 await classicSecondary.locator('button[aria-current=page]').filter({hasText: '项目'}).waitFor();
 await classicSecondary.getByRole('button', {name: '收起侧边栏', exact: true}).click();
 await more();
 await page.getByRole('menuitem', {name: '项目', exact: true}).click();
 await classicSecondary.locator('button[aria-current=page]').filter({hasText: '项目'}).waitFor();
 await page.getByRole('menu').waitFor({state: 'hidden'});
 await page.waitForFunction(() => document.querySelector('.jx-classicModuleShell').getBoundingClientRect().width >= 239);
 await page.screenshot({path: resolve(output, 'classic.png'), animations: 'disabled'});
 await page.evaluate(() => {window.__sidebarNavigation.layout('classic'); window.__sidebarNavigation.collapse(true);});
 await more(); await page.keyboard.press('Escape');
 await page.evaluate(() => {window.__sidebarNavigation.layout('modern'); window.__sidebarNavigation.collapse(false);});
 for (const height of [360, 1000]) {
  await page.setViewportSize({width: 1440, height});
  for (const theme of ['dark', 'light']) {
   await page.evaluate(theme => window.__sidebarNavigation.theme(theme), theme);
   await more(); await page.keyboard.press('Escape');
  }
 }
 await page.getByRole('menu').waitFor({state: 'hidden'});
 await page.screenshot({path: resolve(output, 'modern.png'), animations: 'disabled'});
 await page.setViewportSize({width: 390, height: 844});
 await page.evaluate(() => window.__sidebarNavigation.collapse(false));
 await more();
 await page.getByRole('menuitem', {name: '站点', exact: true}).click();
 await page.evaluate(() => window.__sidebarNavigation.collapse(false));
 await page.locator('.jx-moduleSidebar').getByRole('button', {name: '站点', exact: true}).waitFor();
 assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
 await page.getByRole('menu').waitFor({state: 'hidden'});
 await page.screenshot({path: resolve(output, 'mobile.png'), animations: 'disabled'});
 failDirectory = true;
 const directoryFailure = page.waitForResponse(response => response.url().endsWith('/workbench/catalog') && response.status() === 503);
 await page.evaluate(() => window.__sidebarNavigation.reload());
 await directoryFailure;
 await more(); await page.keyboard.press('Escape');
 assert.equal(await page.getByRole('checkbox').count(), 0, 'layout settings cannot restore app pinning');
 assert.equal(requests.some(path => path.includes('/navigation-preferences')), false, 'module navigation does not read stale application pins');
 assert.deepEqual(errors, []);
 console.log('sidebar navigation: exact More menu, secondary navigation, refresh/collapse, classic/modern, 0/5/100 apps, permissions, app launch and stale policy isolation passed');
} catch (error) {
 console.error('UI:', (await browser.contexts()[0]?.pages()[0]?.locator('body').innerText())?.slice(-2000));
 throw error;
} finally {await browser.close();}
