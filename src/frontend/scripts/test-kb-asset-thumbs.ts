/**
 * 知识库检索结果配图在对话里显示的回归。
 *
 * 盯的是那条容易断在中间、断了也不报错的链路：后端在命中片段上挂了 images，点开引用时
 * 前端要把整条 item 还原出来交给渲染器，渲染器再把它画成缩略图。中间任何一环把 images
 * 吃掉，界面只是"没有图"——不会报错，不会有日志，只会被当成"这篇文档没图"。
 */
import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';

import type { CitationItem } from '../src/types';
import { getCitationOutputSlice } from '../src/utils/citations';
import { extractAssetRefs, resolveAssetUrl } from '../src/components/kb/kbAssets';
import { renderRetrieveLocalKB } from '../src/components/tool/renderers/KBRenderer';

const ASSET_URL = '/api/v1/catalog/kb/assets/abc123';

function kbToolOutput(extra: Record<string, unknown> = {}) {
  return JSON.stringify({
    available_kbs: [{ kb_id: 'kb1', name: '年报库' }],
    items: [
      {
        id: 'chunk_1',
        title: '2026 年报',
        content: '第四季度营收创新高。',
        kb_id: 'kb1',
        score: 0.91,
        ...extra,
      },
    ],
  });
}

{
  // URL 还原：入库时写的是带 /api 前缀的相对链接，不能再叠一层前缀。
  assert.equal(resolveAssetUrl(ASSET_URL), ASSET_URL);
  assert.equal(resolveAssetUrl('https://cdn.test/x.jpg'), 'https://cdn.test/x.jpg');
  assert.equal(resolveAssetUrl(''), '');
}

{
  // 从分块正文里抠链接（分块查看页只有正文，没有结构化 images）
  const refs = extractAssetRefs(`见 ![](${ASSET_URL}) 图。重复的 ![](${ASSET_URL}) 只算一次`);
  assert.equal(refs.length, 1);
  assert.equal(refs[0].url, ASSET_URL);
  assert.equal(extractAssetRefs('没有图的正文').length, 0);
}

{
  // 引用还原必须原样带上 images——这里被裁掉的话，界面就永远没有图
  const citation: CitationItem = {
    id: 'e1',
    tool_name: 'retrieve_local_kb',
    tool_id: 'call_1',
    title: '2026 年报',
    url: '',
    snippet: '',
    source_type: 'knowledge_base',
    item_index: 0,
  };
  const slice = getCitationOutputSlice(citation, [
    {
      id: 'call_1',
      name: 'retrieve_local_kb',
      output: kbToolOutput({
        images: [{ asset_id: 'abc123', url: ASSET_URL, caption: '柱状图：Q4 最高' }],
      }),
    } as never,
  ]);
  const items = (slice.output as { items: Array<{ images?: unknown[] }> }).items;
  assert.equal(items.length, 1);
  assert.ok(items[0].images, '引用还原把 images 丢了');
}

{
  // 渲染：结构化 images 出图，且用 caption 作为可访问名
  const html = renderToStaticMarkup(
    renderRetrieveLocalKB(
      JSON.parse(kbToolOutput({ images: [{ asset_id: 'abc123', url: ASSET_URL, caption: '柱状图：Q4 最高' }] })),
      () => {}
    ) as React.ReactElement
  );
  assert.ok(html.includes(`src="${ASSET_URL}"`), `缩略图没渲染出来: ${html}`);
  assert.ok(html.includes('柱状图：Q4 最高'), '图注没带上');
}

{
  // 没有结构化 images 的历史命中，退回从正文抠链接，同样要出图
  const html = renderToStaticMarkup(
    renderRetrieveLocalKB(
      JSON.parse(
        JSON.stringify({
          items: [{ id: 'c1', title: '旧文档', content: `见 ![](${ASSET_URL})`, score: 0.5 }],
        })
      ),
      () => {}
    ) as React.ReactElement
  );
  assert.ok(html.includes(`src="${ASSET_URL}"`), '历史命中没有回退到正文抠链接');
}

{
  // 没有图的命中不能凭空多出一个空容器
  const html = renderToStaticMarkup(
    renderRetrieveLocalKB(JSON.parse(kbToolOutput()), () => {}) as React.ReactElement
  );
  assert.ok(!html.includes('jx-kbAssetThumbs'), '无图命中不该渲染缩略图容器');
}

console.log('✅ KB 资产缩略图链路通过');
