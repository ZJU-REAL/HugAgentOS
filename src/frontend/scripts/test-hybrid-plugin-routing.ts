/**
 * 混合模式下插件请求的分流：**装什么由云端定，开不开由本机定**。
 *
 * 本机只答两件事——本机这份已装清单（含详情、界面贡献）长什么样、某个插件开还是关。
 * 市场列表与详情、安装、导入、卸载、展示信息都是云端账号上的事；打到本机会在本机装出
 * 第二份，那份的连接器指向本机没人监听的端口，跑不通，还会和云端同步下来的那份在能力
 * 中心并排显示。
 */
import assert from 'node:assert/strict';

import {
  getInstalledPluginDetail,
  installPlugin,
  listInstalledPlugins,
  listPluginUiContributions,
  listPlugins,
  setCapabilityPlaneReady,
  setHybridDual,
  setInstalledPluginMeta,
  setPluginEnabled,
  uninstallPlugin,
} from '../src/api';

const requests: Array<{ url: string; init?: RequestInit }> = [];
globalThis.fetch = async (url, init) => {
  requests.push({ url: String(url), init });
  return new Response(JSON.stringify({ code: 200, data: { items: [] } }), { status: 200 });
};

setHybridDual(true);
setCapabilityPlaneReady(true);

async function targetOf(call: () => Promise<unknown>): Promise<string | null> {
  requests.length = 0;
  await call();
  assert.equal(requests.length, 1, '每个用例只应发出一次请求');
  return new Headers(requests[0].init?.headers).get('x-hugagent-target');
}

assert.equal(await targetOf(() => listInstalledPlugins()), 'local', '本机已装清单');
assert.equal(await targetOf(() => listPluginUiContributions()), 'local', '界面贡献跟启停同源');
assert.equal(await targetOf(() => getInstalledPluginDetail('sites@user_1')), 'local', '已装详情');
assert.equal(await targetOf(() => setPluginEnabled('sites@user_1', false)), 'local', '启停归本机');

assert.equal(await targetOf(() => listPlugins()), null, '市场列表只在云端');
assert.equal(await targetOf(() => installPlugin('sites')), null, '安装只在云端');
assert.equal(await targetOf(() => uninstallPlugin('sites@user_1')), null, '卸载只在云端');
assert.equal(
  await targetOf(() => setInstalledPluginMeta('sites@user_1', { display_name: 'x' })),
  null,
  '展示信息是账号资产的属性',
);

// 非混合形态下这套分流整体不生效：web 与纯云端壳照旧全打云端。
setHybridDual(false);
setCapabilityPlaneReady(false);
assert.equal(await targetOf(() => listInstalledPlugins()), null, '非双模式不打本机头');

console.log('hybrid plugin routing: ok');
