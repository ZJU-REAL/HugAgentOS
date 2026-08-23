/**
 * 插件 UI 字段映射求值器自测（无浏览器依赖，node 直接跑）。
 *
 * 这是整个 L0 声明式层的安全底线：插件的 map 是**数据**，不是代码。求值器必须是
 * 白名单解析器——认得的形状才求值，其余一律返回 undefined，绝不出现"把声明当表达式跑"
 * 的口子。所以这里除了正常取值，重点钉三件事：
 *   1. 越界写法（下标、表达式、原型链）一律不解析；
 *   2. 上游信封（result / 结果 / 数据 嵌套 + JSON 字符串）能被 unwrap 正确剥开；
 *   3. actions 的 $root / $node / $item 三个作用域各取各的，不串味。
 */
import { readFileSync } from 'fs';
import { resolve as resolvePath } from 'path';

import {
  readNumber,
  readPointer,
  readRecords,
  readText,
  resolveParams,
  unwrap,
} from '../src/plugin-ui/pointer';

let failed = 0;
function check(name: string, cond: boolean, extra?: unknown) {
  if (cond) {
    console.log(`  ok   ${name}`);
  } else {
    failed += 1;
    console.error(`  FAIL ${name}`, extra ?? '');
  }
}

// ── ① 基本取值 ────────────────────────────────────────────────
{
  const data = {
    名称: '锂电池',
    产业链概况: { 企业数量: 1280 },
    items: [{ 标题: 'A', cite_id: 'e1' }, { 标题: 'B' }],
  };
  check('$ 取整体', readPointer(data, '$') === data);
  check('$.field 取顶层', readText(data, '$.名称') === '锂电池');
  check('$.a.b 取嵌套', readNumber(data, '$.产业链概况.企业数量') === 1280);
  check('$.items[] 取数组', readRecords(data, '$.items[]').length === 2);
  check(
    '$.items[].标题 列映射',
    JSON.stringify(readPointer(data, '$.items[].标题')) === '["A","B"]',
  );
  check('缺失字段返回 undefined', readPointer(data, '$.不存在') === undefined);
  check('多候选取第一个命中', readText(data, ['$.没有', '$.名称']) === '锂电池');
}

// ── ② 越界写法一律不解析（安全底线） ──────────────────────────
{
  const data = { items: [{ a: 1 }], __proto__: { polluted: true } } as Record<string, unknown>;
  const rejected = [
    '$.items[0]',              // 下标
    '$.items[0].a',
    '$.a + 1',                 // 表达式
    '${constructor}',
    '$.a; fetch("//x")',       // 语句注入
    'items',                   // 不以 $ 开头
    '$..a',                    // 空段
    '$.',
    '$.a[]extra',
  ];
  for (const spec of rejected) {
    check(`拒绝越界写法 ${spec}`, readPointer(data, spec) === undefined, readPointer(data, spec));
  }
  // 原型链上的属性不该被当作数据取到
  check('不读原型链属性', readPointer({} as Record<string, unknown>, '$.polluted') === undefined);
  check('不读内置方法', readPointer({} as Record<string, unknown>, '$.constructor') === undefined);
}

// ── ③ unwrap 剥信封 ──────────────────────────────────────────
{
  const wrapped = { result: JSON.stringify({ 结果: { 数据: { 名称: '整车' } } }) };
  const out = unwrap(wrapped, ['result', '结果', '数据']);
  check('剥多层信封 + JSON 字符串', readText(out, '$.名称') === '整车');

  const plain = { 名称: '直给' };
  check('没有信封时原样返回', unwrap(plain, ['result']) === plain);

  const partial = { result: { 名称: '一层' } };
  check('剥一层就停', readText(unwrap(partial, ['result', '结果']), '$.名称') === '一层');

  check('unwrap 不传 keys 只解 JSON', readText(unwrap('{"名称":"J"}', undefined), '$.名称') === 'J');
}

// ── ④ 数字容错（上游数字常带装饰） ────────────────────────────
{
  check('带千分位', readNumber({ v: '1,234' }, '$.v') === 1234);
  check('带百分号', readNumber({ v: '23.5%' }, '$.v') === 23.5);
  check('带单位', readNumber({ v: '12亿元' }, '$.v') === 12);
  check('非数字返回 undefined', readNumber({ v: '暂无' }, '$.v') === undefined);
  check('原生数字', readNumber({ v: 42 }, '$.v') === 42);
}

// ── ⑤ actions 的三个作用域各取各的 ───────────────────────────
{
  const root = { chain_id: 'c-1', 名称: '根' };
  const node = { node_id: 'n-9', 名称: '节点' };
  const item = { 企业名称: '某某公司' };

  const params = resolveParams(
    {
      chainId: '$root.chain_id',
      nodeId: '$node.node_id',
      company: '$item.企业名称',
      bare: '$.chain_id',      // 无作用域前缀 = 取 root
      literal: 'province',     // 非 $ 开头 = 字面量
    },
    { root, node, item },
  );

  check('$root 取根', params.chainId === 'c-1');
  check('$node 取节点', params.nodeId === 'n-9');
  check('$item 取条目', params.company === '某某公司');
  check('裸 $ 取根', params.bare === 'c-1');
  check('字面量原样透传', params.literal === 'province');
  check('作用域不串味', params.nodeId !== root.chain_id && params.company !== node.node_id);

  // 作用域缺席时不该抛，也不该塞进无意义的 undefined
  const partial = resolveParams({ nodeId: '$node.node_id' }, { root });
  check('作用域缺席时略过该参数', !('nodeId' in partial), partial);
}

// ── ⑥ 真实清单 × 生产端载荷形状（钉住"产业链画布只剩根节点"事故） ──────────
// 字段名一律以 MCP 生产端（impl_chain.py）与退役解析器（industryChain.ts）为准：
// 下级环节 / 节点ID / 企业数 / 产业链概况.产业链ID。清单里的指针猜错任何一个，
// 树就只剩根节点——正是线上出过的问题，所以这里直接加载真实 plugin.json 来验。
{
  const manifest = JSON.parse(readFileSync(
    // npm script 固定以 src/frontend 为 cwd；打包产物在 node_modules/.tmp 下，ESM 里也没有 __dirname
    resolvePath(process.cwd(), '../backend/plugin_bundles/marketplace/industry-knowledge-center/plugin.json'),
    'utf-8',
  ));
  const canvas = manifest.extensions['org.hugagent'].ui.contributes.canvas_views[0];
  const map = canvas.map as Record<string, unknown>;

  // 生产端真实形状的最小样例（含 result 信封，模拟工具输出原文）
  const payload = {
    result: {
      产业链概况: { 产业链ID: 'industry_humanoid', 名称: '人形机器人' },
      产业链图谱: {
        名称: '人形机器人',
        下级环节: [
          {
            名称: '上游核心零部件',
            下级环节: [
              { 名称: '伺服电机', 节点ID: 'node_a1', 企业数: 128 },
            ],
          },
          { 名称: '下游整机', 节点ID: 'node_b1', 企业数: 56 },
        ],
      },
    },
  };

  const data = unwrap(payload, canvas.unwrap);
  const root = readPointer(data, map.root as string) as Record<string, unknown>;
  check('清单 unwrap 剥开 result 信封后能取到图谱根', !!root && typeof root === 'object');
  check('根节点名称', readText(root, map.label) === '人形机器人');

  const level1 = readRecords(root, map.children);
  check('一级环节解析出 2 个', level1.length === 2, level1.length);

  const level2 = readRecords(level1[0], map.children);
  check('二级环节解析出 1 个', level2.length === 1, level2.length);
  check('末级节点 ID', readText(level2[0], map.node_id) === 'node_a1');
  check('企业数徽章', readNumber(level2[0], map.badge) === 128);

  // 画布头部：标题与关键指标都来自 产业链概况
  check('画布标题从概况取', readText(data, map.title) === '人形机器人');

  // 节点动作参数：chainId 藏在 产业链概况 里，nodeId 来自被点节点
  const action = canvas.actions[0];
  const params = resolveParams(action.params, { root: data, node: level2[0] });
  check('action chainId 从概况取', params.chainId === 'industry_humanoid', params);
  check('action nodeId 从节点取', params.nodeId === 'node_a1', params);

  // 钻取面板结果映射：对 provider（ui_data.node_companies）归一化后的固定形状断言，
  // 该形状与旧 /v1/industry/.../companies 路由完全一致——字段再猜错这里会先红。
  const rmap = action.result.map as Record<string, unknown>;
  const providerShaped = {
    items: [{
      id: 'c1',
      name: '某某精密传动股份公司',
      labels: ['专精特新', '高新技术'],
      belong_area: '华东地区',
      establish_date: '2015-06-01',
      registered_capital: '5000万元',
      detail_url: 'https://example.com/analysis/knowledge/company?id=c1',
    }],
    pagination: { page: 1, page_size: 10, total_items: 128, total_pages: 13 },
  };
  const rows = readRecords(providerShaped, rmap.items);
  check('企业列表条目', rows.length === 1);
  check('企业名称', readText(rows[0], rmap.title) === '某某精密传动股份公司');
  check('资质标签数组', (readPointer(rows[0], rmap.tags as string) as unknown[]).length === 2);
  check('所属地区', readText(rows[0], rmap.region) === '华东地区');
  check('详情链接', readText(rows[0], rmap.link).startsWith('https://'));
  check('总数指针', readNumber(providerShaped, rmap.total) === 128);
  check('总页数指针', readNumber(providerShaped, rmap.total_pages) === 13);
}

if (failed > 0) {
  console.error(`\n插件 UI 字段映射自测失败 ${failed} 项`);
  process.exit(1);
}
console.log('\n插件 UI 字段映射自测全部通过');
