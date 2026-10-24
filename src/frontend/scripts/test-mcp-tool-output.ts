/**
 * MCP 工具结果外壳自测（无浏览器依赖，node 直接跑）。
 *
 * 2026-10-22 起有一段历史把 MCP 结果存成原始 CallToolResult，搜索卡片因此显示
 * 「暂无搜索结果」。这里盯住：外壳要拆成工具自己的值，其它形状一律不动。
 */
import { unwrapMcpResult } from '../src/utils/mcpResult';
import { extractArtifactOutputs } from '../src/utils/fileParser';

let failed = 0;
function check(name: string, cond: boolean, extra?: unknown) {
  if (cond) {
    console.log(`  ok   ${name}`);
  } else {
    failed += 1;
    console.error(`  FAIL ${name}`, extra ?? '');
  }
}

const search = { result: { status: 'complete', results: [{ title: 'Doc', url: 'https://example.org' }] } };

const clipped = unwrapMcpResult({
  _meta: {},
  content: [{ type: 'text', text: '{"result": {"status": "complete", 〔截断' }],
  structuredContent: search,
  isError: false,
  resultType: 'complete',
});
check('clipped text falls back to structuredContent, single result key unwrapped', clipped === search.result, clipped);

const full = unwrapMcpResult({
  content: [{ type: 'text', text: JSON.stringify(search) }],
  structuredContent: search,
  isError: false,
});
check('complete text JSON is what the card renders', JSON.stringify(full) === JSON.stringify(search), full);

// typing.Dict / str returns: the SDK wraps structuredContent, the text holds the tool's own value.
const datasets = { public_datasets: [], private_datasets: [], total: 0 };
const wrapped = unwrapMcpResult({
  content: [{ type: 'text', text: JSON.stringify(datasets) }],
  structuredContent: { result: datasets },
  isError: false,
});
check('SDK-wrapped tool keeps the unwrapped text shape', JSON.stringify(wrapped) === JSON.stringify(datasets), wrapped);

const wrappedString = unwrapMcpResult({
  content: [{ type: 'text', text: '✅ 查询成功〔截断' }],
  structuredContent: { result: '✅ 查询成功 full' },
  isError: false,
});
check('wrapped string stays {result: text}', JSON.stringify(wrappedString) === JSON.stringify({ result: '✅ 查询成功 full' }), wrappedString);

const plain = unwrapMcpResult({ content: [{ type: 'text', text: 'hello' }], isError: true });
check('non-JSON text without structured value becomes {result: text}', JSON.stringify(plain) === '{"result":"hello"}', plain);

const image = { content: [{ type: 'image', data: 'x', mimeType: 'image/png' }], isError: false };
check('envelope without text or structured value is kept', unwrapMcpResult(image) === image);

const toolOwn = { content: [{ type: 'text', text: 'not an envelope' }], title: 'doc' };
check('tool payload with a content array but no isError is kept', unwrapMcpResult(toolOwn) === toolOwn);
check('plain values pass through', unwrapMcpResult(search) === search && unwrapMcpResult('s') === 's');

const artifacts = extractArtifactOutputs({
  content: [],
  isError: false,
  structuredContent: { result: { file_id: 'f1', url: '/files/f1', name: 'a.docx' } },
});
check('artifacts are found inside an MCP envelope', artifacts.length === 1 && artifacts[0].file_id === 'f1', artifacts);

if (failed) {
  console.error(`\n${failed} check(s) failed`);
  process.exit(1);
}
console.log('\nall MCP tool output checks passed');
