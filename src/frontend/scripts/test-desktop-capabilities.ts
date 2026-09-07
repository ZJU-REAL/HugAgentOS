import assert from 'node:assert/strict';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createDeviceMcpStore } from '../src/stores/deviceMcpState';
import { createDeviceSkillEditorStore } from '../src/stores/deviceSkillEditorState';
import { createDesktopCapabilityStore, type DesktopCapabilityClient } from '../src/stores/desktopCapabilityState';
import { deviceMcpSpec } from '../src/utils/deviceMcpForm';
import { DeviceCapabilityChoice } from '../src/components/catalog/DeviceCapabilityChoice';
import { deviceCapabilityState, deviceDependencyReason, canPrepareDeviceCapability, canRemoveDeviceFiles, canCreateDeviceLocalCopy, cloudRestoreTarget, canToggleDeviceCapability } from '../src/utils/deviceCapabilities';
import { getDeviceCapabilities, putDeviceLocalMcp, deleteDeviceLocalMcp, repairDeviceMcpJson, createDeviceLocalCopy, getDeviceSkillFile, putDeviceSkillFile, setDeviceCapabilityEnabled, setDeviceManagedMcpEnabled } from '../src/api';
import { ApiResponseError } from '../src/utils/apiError';
import type { DeviceCapabilityItem, DeviceCapabilityKind, DeviceCapabilityListing, DeviceMcpJson } from '../src/api';

const item = (over: Partial<DeviceCapabilityItem> = {}): DeviceCapabilityItem => ({
  install_id: 'skill:account:report', runtime_name: 'report', kind: 'skill',
  profile: 'account', source: 'cloud', state: 'ready', usable: true, enabled: true, registered: true,
  revision: 'v1', resolution: { outcome: 'chosen', reason: 'account_unique' }, ...over,
});
const listing = (kind: DeviceCapabilityKind, items: DeviceCapabilityItem[] = []): DeviceCapabilityListing => ({
  kind, profile_id: 'account', items, conflicts: {}, preferences: {},
});

async function run() {
  const localSpec = deviceMcpSpec({ server_id: 'workspace', transport: 'stdio', command: 'node',
    args_text: 'server.js\n--readonly', env_text: '{"WORKSPACE":"/work"}' });
  assert.deepEqual(localSpec.args, ['server.js', '--readonly']);
  assert.deepEqual(localSpec.env, { WORKSPACE: '/work' });
  assert.equal('url' in localSpec, false);
  const httpSpec = deviceMcpSpec({ server_id: 'remote', transport: 'streamable_http',
    url: 'https://example.com/mcp', secret_headers_text: '' });
  assert.equal('secret_headers' in httpSpec, false, '编辑时空认证字段不覆盖现存认证');
  assert.throws(() => deviceMcpSpec({ server_id: 'bad', transport: 'stdio', command: 'node', env_text: '[]' }));
  assert.throws(() => deviceMcpSpec({ server_id: 'bad', transport: 'sse', url: 'file:///etc/passwd' }));

  const requests: Array<{ url: string; init?: RequestInit }> = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    requests.push({ url: String(url), init });
    return new Response(JSON.stringify({ data: {} }), { status: 200 });
  };
  try {
    await getDeviceCapabilities('agent');
    await putDeviceLocalMcp('my tool', { transport: 'stdio', enabled: true, command: 'node',
      expected_generation: 8, expected_digest: 'digest-a' });
    await deleteDeviceLocalMcp('my tool', { expected_generation: 8, expected_digest: 'digest-a' });
    await repairDeviceMcpJson();
    await createDeviceLocalCopy('skill:cloud:report');
    await getDeviceSkillFile('skill:local:report');
    await putDeviceSkillFile('skill:local:report', '# Local draft', 'rev-before');
    await setDeviceCapabilityEnabled('skill:cloud:report', false);
    await setDeviceManagedMcpEnabled('p_' + 'a'.repeat(32), 'tool/with space', false);
    for (const req of requests) assert.equal(new Headers(req.init?.headers).get('x-hugagent-target'), 'local');
    assert.match(requests[1].url, /my%20tool$/);
    assert.deepEqual(JSON.parse(String(requests[1].init?.body)).expected_digest, 'digest-a');
    assert.match(requests[2].url, /expected_generation=8/);
    assert.match(requests[2].url, /expected_digest=digest-a/);
    assert.equal(requests[3].init?.method, 'POST');
    assert.match(requests[4].url, /installations\/skill%3Acloud%3Areport\/local-copy$/);
    assert.equal(requests[4].init?.method, 'POST');
    assert.match(requests[5].url, /installations\/skill%3Alocal%3Areport\/files\/SKILL.md$/);
    assert.equal(requests[6].init?.method, 'PUT');
    assert.deepEqual(JSON.parse(String(requests[6].init?.body)), { content: '# Local draft', expected_revision: 'rev-before' });
    assert.equal(requests[7].init?.method, 'PUT');
    assert.match(requests[7].url, /installations\/skill%3Acloud%3Areport\/enabled$/);
    assert.deepEqual(JSON.parse(String(requests[7].init?.body)), { enabled: false });
    assert.match(requests[8].url, /managed\/p_a{32}\/tool%2Fwith%20space\/enabled$/);
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: { code: 'mcp_json_conflict', message: 'changed' } }), { status: 409 });
    await assert.rejects(putDeviceLocalMcp('x', { transport: 'stdio', enabled: true, command: 'node' }),
      (error: unknown) => error instanceof ApiResponseError && error.status === 409);
  } finally { globalThis.fetch = realFetch; }

  // 已就绪插件/Agent、被撤权项、文件损坏各自保留真实状态；MCP引用不能显示文件操作。
  for (const kind of ['agent', 'plugin'] as const) {
    assert.equal(deviceCapabilityState(item({ kind })).text, '已就绪');
    assert.equal(deviceCapabilityState(item({ kind, usable: false, enabled: true })).text, '当前不可用');
    assert.equal(deviceCapabilityState(item({ kind, usable: false, enabled: false })).text, '已停用');
  }
  assert.equal(canPrepareDeviceCapability(item({ state: 'files_missing' })), true);
  assert.equal(canPrepareDeviceCapability(item({ kind: 'mcp', state: 'failed' })), false);
  assert.equal(canRemoveDeviceFiles(item({ kind: 'mcp' })), false);
  assert.equal(canPrepareDeviceCapability(item({ kind: 'plugin', readiness: {
    ready: false, missing_required: ['skill:account:part'], components: [],
  } })), true);

  for (const [state, text] of [['pending', '待下载'], ['files_missing', '文件缺失'],
    ['failed', '准备失败'], ['schema_empty', '暂无工具定义']] as const) {
    assert.equal(deviceCapabilityState(item({ state, usable: false,
      readiness: { ready: false, missing_required: [], components: [] } })).text, text,
      'readiness must not hide the concrete package or schema state');
  }

  // 已校验文件的技能/Agent 可重新检查依赖，不能被当作可运行或要求重新下载。
  for (const kind of ['skill', 'agent', 'plugin'] as const) {
    const retained = item({ kind, files_ready: true, usable: false,
      resolution: { outcome: 'unusable', reason: 'dependency_missing' },
      readiness: { ready: false, missing_required: ['cli_missing:python'], components: [] } });
    assert.equal(canPrepareDeviceCapability(retained), true, kind + ' exposes dependency retry');
    assert.equal(deviceCapabilityState(retained).text, '组件未就绪');
    assert.equal(canRemoveDeviceFiles(retained), true, 'retained files remain removable');
    assert.equal(canPrepareDeviceCapability({ ...retained, source: 'local' }), false, 'cloud preparation never guesses a local install protocol');
  }

  assert.equal(deviceDependencyReason('platform_incompatible: skill:p_abc:windows-tool', (text) => text),
    '当前操作系统不兼容: skill:p_abc:windows-tool');
  assert.equal(deviceDependencyReason('runtime_dependency_missing: cli:python', (text) => text),
    '缺少运行依赖: cli:python');
  assert.equal(deviceDependencyReason('runtime_command_missing: mcp:local:server', (text) => 'EN[' + text + ']'),
    'EN[找不到本机命令]: mcp:local:server');
  assert.equal(deviceDependencyReason('new_reason: skill:x', (text) => text), 'new_reason: skill:x',
    'unknown reasons stay inspectable instead of being silently replaced');
  assert.equal(deviceDependencyReason('skill:account:part', (text) => text), 'skill:account:part');

  // 已选云端项时，本机同名候选仍有明确选择入口，用户也能恢复自动选择。
  const cloud = item({ display_name: 'Cloud report' });
  const local = item({ install_id: 'skill:local:report', profile: 'local', source: 'local',
    display_name: 'Local report', resolution: { outcome: 'shadowed', reason: 'name_preference' } });
  const html = renderToStaticMarkup(createElement(DeviceCapabilityChoice, {
    kind: 'skill', runtimeName: 'report', candidates: [cloud, local], preference: cloud.install_id, onChoose: () => {},
  }));
  assert.ok(html.includes('skill:local:report') && html.includes('Local report'));
  assert.ok(html.includes('skill:account:report') && html.includes('Cloud report'));
  assert.ok(html.includes('自动选择'));
  let selected: string | null | undefined;
  const choice = DeviceCapabilityChoice({ kind: 'skill', runtimeName: 'report', candidates: [cloud, local],
    preference: cloud.install_id, onChoose: (id) => { selected = id; } });
  assert.ok(choice);
  choice.props.onChange({ stopPropagation() {}, target: { value: local.install_id } });
  assert.equal(selected, local.install_id);
  choice.props.onChange({ stopPropagation() {}, target: { value: '' } });
  assert.equal(selected, null);

  const copy = item({ install_id: 'skill:local:report-copy', source: 'local', profile: 'local',
    derived_from: cloud.install_id, resolution: { outcome: 'chosen', reason: 'name_preference' } });
  assert.equal(canCreateDeviceLocalCopy(cloud), true);
  for (const kind of ['agent', 'plugin', 'mcp'] as const) assert.equal(canCreateDeviceLocalCopy(item({ kind })), false);
  assert.equal(canCreateDeviceLocalCopy(item({ state: 'pending' })), false);
  assert.equal(cloudRestoreTarget(copy, [copy, cloud])?.install_id, cloud.install_id);
  assert.equal(cloudRestoreTarget(copy, [copy]), null, '已撤销的云端原版没有恢复入口');
  assert.equal(cloudRestoreTarget(copy, [copy, item({ usable: false })]), null);
  assert.equal(deviceCapabilityState({ ...cloud, resolution: { outcome: 'shadowed', reason: 'name_preference' } }, [cloud, copy]).text, '已被本机副本取代');

  let rows = [cloud];
  const calls: string[] = [];
  let deferred: ((v: DeviceCapabilityListing) => void) | null = null;
  const client: DesktopCapabilityClient = {
    setEnabled: async (id, enabled) => {
      calls.push('enabled:' + id + ':' + enabled);
      rows = rows.map((entry) => entry.install_id === id ? { ...entry, enabled, usable: enabled,
        resolution: { outcome: enabled ? 'chosen' as const : 'unusable' as const, reason: null } } : entry);
      return listing('skill', rows);
    },
    list: async (kind) => { calls.push('list:' + kind); return listing(kind, kind === 'skill' ? rows : []); },
    sync: async () => { calls.push('sync'); return {}; },
    prepare: async () => [{ install_id: cloud.install_id, ok: false, error: { code: 'download_failed', message: 'retry' } }],
    remove: async () => {},
    copyLocal: async (id) => { calls.push('copy:' + id); rows = [cloud, copy]; return { install_id: copy.install_id, installation: copy }; },
    choose: async (kind, name, id) => ({ ...listing(kind, rows), preferences: id ? { [name]: id } : {} }),
  };
  const store = createDesktopCapabilityStore(client, () => true);
  await store.getState().load('skill');
  rows = [cloud, item({ install_id: 'skill:account:new', runtime_name: 'new', state: 'pending' })];
  await store.getState().refresh();
  assert.equal(store.getState().kinds.skill.listing?.items.length, 2, '云端安装后可刷新出待准备项');
  assert.ok(calls.includes('sync') && calls.includes('list:agent') && calls.includes('list:plugin'));

  await assert.rejects(store.getState().prepare('skill', [cloud.install_id]), /retry/);
  assert.deepEqual(store.getState().busy, {}, '准备失败也清理忙碌状态');
  await store.getState().copyLocal('skill', cloud.install_id);
  assert.ok(calls.includes('copy:' + cloud.install_id));
  assert.ok(store.getState().kinds.skill.listing?.items.some((row) => row.derived_from === cloud.install_id));
  assert.deepEqual(store.getState().busy, {});
  await store.getState().choose('skill', 'report', cloudRestoreTarget(copy, rows)!.install_id);
  assert.equal(store.getState().kinds.skill.listing?.preferences.report, cloud.install_id, '恢复显式绑定云端原版');
  assert.ok(store.getState().kinds.skill.listing?.items.some((row) => row.install_id === copy.install_id), '恢复保留本机副本');
  client.copyLocal = async () => { throw new Error('copy failed'); };
  await assert.rejects(store.getState().copyLocal('skill', cloud.install_id), /copy failed/);
  assert.deepEqual(store.getState().busy, {});
  await store.getState().choose('skill', 'report', local.install_id);
  assert.equal(store.getState().kinds.skill.listing?.preferences.report, local.install_id);
  await store.getState().choose('skill', 'report', null);
  assert.deepEqual(store.getState().kinds.skill.listing?.preferences, {});

  // 切换账号时不能把旧账号尚未返回的本机清单写回新会话。
  client.list = () => new Promise((resolve) => { deferred = resolve; });
  const oldRequest = store.getState().load('skill', true);
  store.getState().reset();
  assert.ok(deferred);
  (deferred as (v: DeviceCapabilityListing) => void)(listing('skill', [cloud]));
  await oldRequest;
  assert.equal(store.getState().kinds.skill.listing, null);

  // 强制刷新在已有读取完成后再次取数，不被 loading 短路吞掉。
  client.list = async (kind) => listing(kind, rows);
  await store.getState().load('skill', true);
  assert.equal(store.getState().kinds.skill.listing?.items.length, 2);
  let release: ((value: DeviceCapabilityListing) => void) | undefined;
  let reads = 0;
  client.list = (kind) => {
    reads += 1;
    return reads === 1 ? new Promise((resolve) => { release = resolve; }) : Promise.resolve(listing(kind, []));
  };
  const firstRead = store.getState().load('skill', true);
  const forcedRead = store.getState().load('skill', true);
  assert.ok(release);
  release(listing('skill', [cloud]));
  await Promise.all([firstRead, forcedRead]);
  assert.equal(reads, 2);
  assert.deepEqual(store.getState().kinds.skill.listing?.items, []);
  for (const kind of ['skill', 'agent', 'plugin'] as const) {
    assert.equal(canToggleDeviceCapability(item({ kind, registered: true })), true);
    assert.equal(canToggleDeviceCapability(item({ kind, registered: false })), false);
    assert.equal(canToggleDeviceCapability(item({ kind, source: 'builtin' })), false);
  }
  assert.equal(canToggleDeviceCapability(item({ kind: 'mcp' })), false);
  client.list = async (kind) => listing(kind, kind === 'skill' ? rows : []);
  rows = [cloud];
  await store.getState().setEnabled('skill', cloud.install_id, false);
  assert.equal(store.getState().kinds.skill.listing?.items[0]?.enabled, false);
  assert.equal(deviceCapabilityState(store.getState().kinds.skill.listing!.items[0]).text, '已停用');
  await store.getState().load('skill', true);
  assert.equal(store.getState().kinds.skill.listing?.items[0]?.enabled, false, 'refresh preserves explicit device disable');
  await store.getState().setEnabled('skill', cloud.install_id, true);
  assert.equal(store.getState().kinds.skill.listing?.items[0]?.enabled, true);
  client.setEnabled = async () => {
    rows = [{ ...cloud, enabled: false, usable: false }];
    return listing('skill', rows);
  };
  await store.getState().setEnabled('skill', cloud.install_id, true);
  assert.equal(store.getState().kinds.skill.listing?.items[0]?.enabled, false,
    'an enable preference cannot pretend the server restored cloud authorization');
  rows = [cloud];
  await store.getState().load('skill', true);
  client.setEnabled = async () => { throw new Error('enable rejected'); };
  await assert.rejects(store.getState().setEnabled('skill', cloud.install_id, false), /enable rejected/);
  assert.equal(store.getState().busy[cloud.install_id], undefined);
  assert.equal(store.getState().kinds.skill.listing?.items[0]?.enabled, true);

  // 依赖失败后的刷新保留 files_ready，不伪造成功；再次检查只接受服务端新投影。
  let retryItem = item({ files_ready: true, usable: false,
    resolution: { outcome: 'unusable', reason: 'dependency_missing' },
    readiness: { ready: false, missing_required: ['cli_missing:python'], components: [] } });
  let dependencyAvailable = false;
  const retryStore = createDesktopCapabilityStore({
    ...client,
    list: async (kind) => listing(kind, kind === 'skill' ? [retryItem] : []),
    prepare: async () => {
      if (!dependencyAvailable) return [{ install_id: retryItem.install_id, ok: false,
        files_ready: true, readiness: retryItem.readiness,
        error: { code: 'dependency_missing', message: 'Files retained; dependency unavailable', recovery_action: 'inspect_dependencies' } }];
      retryItem = { ...retryItem, usable: true,
        readiness: { ready: true, missing_required: [], components: [] },
        resolution: { outcome: 'chosen', reason: null } };
      return [{ install_id: retryItem.install_id, ok: true, files_ready: true,
        readiness: retryItem.readiness, installation: retryItem }];
    },
  }, () => true);
  await retryStore.getState().load('skill');
  await assert.rejects(retryStore.getState().prepare('skill', [retryItem.install_id]), /Files retained/);
  assert.equal(retryStore.getState().kinds.skill.listing?.items[0].files_ready, true);
  assert.equal(retryStore.getState().kinds.skill.listing?.items[0].usable, false);
  assert.deepEqual(retryStore.getState().busy, {});
  dependencyAvailable = true;
  await retryStore.getState().prepare('skill', [retryItem.install_id]);
  assert.equal(retryStore.getState().kinds.skill.listing?.items[0].usable, true);
  assert.equal(deviceCapabilityState(retryStore.getState().kinds.skill.listing!.items[0]).text, '已就绪');

  // The same editor controller is used by the actual modal: stale writes preserve
  // the draft and baseline; successful saves refresh the installation projection.
  let fileRevision = 'rev-before';
  let writes = 0;
  let refreshed = 0;
  const localFile = () => ({ filename: 'SKILL.md', content: '# Original copy', revision: fileRevision, is_binary: false });
  const editor = createDeviceSkillEditorStore(local.install_id, {
    read: async () => localFile(),
    write: async (id, content, revision) => {
      assert.equal(id, local.install_id);
      assert.equal(content, '# Edited local copy');
      assert.equal(revision, 'rev-before');
      writes += 1;
      if (writes === 1) throw new Error('Transient save failure');
      fileRevision = 'rev-after';
      return { ...localFile(), content };
    },
  }, async () => { refreshed += 1; });
  await editor.getState().load();
  editor.getState().setContent('# Edited local copy');
  await editor.getState().save();
  assert.equal(editor.getState().content, '# Edited local copy');
  assert.equal(editor.getState().file?.revision, 'rev-before');
  assert.ok(editor.getState().error);
  assert.equal(editor.getState().loading, false);
  await editor.getState().save();
  assert.equal(editor.getState().file?.revision, 'rev-after');
  assert.equal(editor.getState().error, null);
  assert.equal(refreshed, 1);
  const conflictEditor = createDeviceSkillEditorStore(local.install_id, {
    read: async () => localFile(),
    write: async () => { throw new ApiResponseError('changed elsewhere', 409, { detail: { code: 'install_conflict' } }); },
  }, async () => { throw new Error('must not refresh failed write'); });
  await conflictEditor.getState().load();
  conflictEditor.getState().setContent('# Keep my draft');
  await conflictEditor.getState().save();
  assert.equal(conflictEditor.getState().content, '# Keep my draft');
  assert.equal(conflictEditor.getState().file?.revision, 'rev-after');
  assert.ok(conflictEditor.getState().error instanceof ApiResponseError);
  let releaseFile: ((file: ReturnType<typeof localFile>) => void) | undefined;
  const staleEditor = createDeviceSkillEditorStore(local.install_id, {
    read: () => new Promise((resolve) => { releaseFile = resolve; }),
    write: async () => localFile(),
  }, async () => {});
  const pendingFile = staleEditor.getState().load();
  staleEditor.getState().dispose();
  releaseFile!(localFile());
  await pendingFile;
  assert.equal(staleEditor.getState().file, null, 'closing or changing editor ignores stale file responses');


  assert.equal(deviceCapabilityState(item({ kind: 'mcp', state: 'schema_empty', usable: false })).text, '暂无工具定义');
  assert.equal(deviceCapabilityState(item({ kind: 'mcp', state: 'disabled', enabled: false, usable: false })).text, '已停用');

  // The actual MCP modal uses this account-scoped store. Switching accounts must
  // reject both old reads and old mutations before publishing/refreshing state.
  const mcpDoc = (profile: string): DeviceMcpJson => ({ path: '/fixture/mcp.json', generation: 1, digest: profile,
    local: {}, managedProfiles: { [profile]: { cloudInstanceId: 'fixture', catalogRevision: '1',
      servers: { tool: { displayName: profile, enabled: true, executionScope: 'cloud', schemaHash: 'h' } } } } });
  const profileA = 'p_' + 'a'.repeat(32);
  const profileB = 'p_' + 'b'.repeat(32);
  let account = 'A';
  let refreshes = 0;
  let finishRead!: (doc: DeviceMcpJson) => void;
  const mcpA = createDeviceMcpStore({ read: () => new Promise((resolve) => { finishRead = resolve; }),
    refresh: async () => { refreshes += 1; } }, () => account === 'A');
  const oldRead = mcpA.getState().load();
  account = 'B';
  mcpA.getState().reset();
  const mcpB = createDeviceMcpStore({ read: async () => mcpDoc(profileB), refresh: async () => { refreshes += 1; } },
    () => account === 'B');
  await mcpB.getState().load();
  finishRead(mcpDoc(profileA));
  await oldRead;
  assert.equal(mcpA.getState().doc, null);
  assert.deepEqual(Object.keys(mcpB.getState().doc!.managedProfiles), [profileB]);

  account = 'A';
  let finishMutation!: (doc: DeviceMcpJson) => void;
  const oldMutation = mcpA.getState().mutate(() => new Promise((resolve) => { finishMutation = resolve; }));
  account = 'B';
  mcpA.getState().reset();
  finishMutation(mcpDoc(profileA));
  assert.equal(await oldMutation, false);
  assert.equal(refreshes, 0, 'old account mutation cannot refresh current account');
  assert.equal(mcpA.getState().doc, null);
  const baseline = mcpB.getState().doc;
  await assert.rejects(mcpB.getState().mutate(async () => { throw new ApiResponseError('conflict', 409, {}); }),
    (error: unknown) => error instanceof ApiResponseError && error.status === 409);
  assert.equal(mcpB.getState().doc, baseline, 'conflict preserves the version used by the current form');
  assert.ok(mcpB.getState().error);
  await mcpB.getState().load();
  assert.equal(mcpB.getState().error, null);
  assert.equal(await mcpB.getState().mutate(async () => ({ ...mcpDoc(profileB), generation: 2 })), true);
  assert.equal(mcpB.getState().doc?.generation, 2);
  assert.equal(refreshes, 1);
  let failMutation!: (error: Error) => void;
  const lateFailure = mcpB.getState().mutate(() => new Promise((_resolve, reject) => { failMutation = reject; }));
  account = 'A';
  mcpB.getState().reset();
  failMutation(new Error('private old account failure'));
  assert.equal(await lateFailure, false, 'old mutation errors cannot leak into the new modal');
  assert.equal(mcpB.getState().error, null);


  console.log('desktop capability UI/state regressions passed');
}
void run().catch((error) => { console.error(error); process.exitCode = 1; });
