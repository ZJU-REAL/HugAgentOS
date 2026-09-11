/**
 * 站点链接必须是壳指向的后端的绝对地址。
 *
 * 桌面窗口由壳在 127.0.0.1 的随机端口上托管，任何相对路径都会被解析成
 * 127.0.0.1 开头——「打开站点」另开的标签、复制走的链接都会变成只有本机可达的
 * 地址，而站点本身托管在云端、公网地址是现成的。
 */
import assert from 'node:assert/strict';

const CLOUD = 'https://hugagent.quant-chi.com';
const SHELL_ORIGIN = 'http://127.0.0.1:64414';
const WEB_ORIGIN = 'https://hugagent.quant-chi.com';

const g = globalThis as unknown as Record<string, unknown>;
g.window = { location: { origin: SHELL_ORIGIN }, addEventListener() {}, removeEventListener() {} };
(g.window as Record<string, unknown>).__HG_DESKTOP__ = {
  provision_mode: 'dual',
  active_local: false,
  server_base: CLOUD,
  local_base: 'http://127.0.0.1:32201',
};
g.document = { documentElement: {}, addEventListener() {}, removeEventListener() {} };
g.localStorage = {
  getItem: () => null, setItem() {}, removeItem() {},
};

const { stablePublicOrigin, useDeploymentModeStore } = await import(
  '../src/stores/deploymentModeStore'
);

// 桌面双模式：站点地址取壳指向的云端，绝不是承载窗口的本地反代。
assert.equal(stablePublicOrigin(), CLOUD, '桌面端必须给出壳指向的后端地址');
assert.notEqual(stablePublicOrigin(), SHELL_ORIGIN, '不得落回反代随机端口');

// LocalOnly：serverBase 本身就是本机后端，同一条规则即可，无需按站点归属分支。
useDeploymentModeStore.setState({
  isDesktop: true, provisionMode: 'local_only', serverBase: 'http://127.0.0.1:32201',
});
assert.equal(stablePublicOrigin(), 'http://127.0.0.1:32201', 'LocalOnly 用壳指向的本机后端');

// web：页面 origin 本来就是真实后端域名。
useDeploymentModeStore.setState({ isDesktop: false, provisionMode: '', serverBase: '' });
(g.window as Record<string, unknown>).location = { origin: WEB_ORIGIN };
assert.equal(stablePublicOrigin(), WEB_ORIGIN, 'web 端用页面 origin');

console.log('site public origin tests passed');
