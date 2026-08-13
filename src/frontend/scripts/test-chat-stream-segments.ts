import assert from 'node:assert/strict';

import type { MessageSegment } from '../src/types';
import {
  appendStreamTextSegment,
  appendThinkingContentBeforeTrailingText,
  deferThinkingTextFragmentBeforeTool,
  restoreDeferredThinkingTextFragment,
} from '../src/utils/streamSegments';
import { extractCodeFromStreamingArgs } from '../src/utils/codeExecParser';
import { buildHistorySegments } from '../src/utils/segments';
import { getToolRunInitialOpen } from '../src/utils/toolRunState';

function tool(toolIndex: number): MessageSegment {
  return { type: 'tool', toolIndex };
}

{
  // Live tool details start expanded. A refreshed/history message is mounted
  // as non-streaming and must start collapsed.
  assert.equal(getToolRunInitialOpen(true), true);
  assert.equal(getToolRunInitialOpen(false), false);
  assert.equal(getToolRunInitialOpen(undefined), false);
}

{
  const segments: MessageSegment[] = [tool(0), { type: 'text', content: '数' }];
  let deferred = deferThinkingTextFragmentBeforeTool(segments, true, undefined);
  segments.push(tool(1));
  deferred = appendStreamTextSegment(segments, '仓确认存在具体数值。', deferred);

  assert.equal(deferred, undefined);
  assert.deepEqual(segments, [
    tool(0),
    tool(1),
    { type: 'text', content: '数仓确认存在具体数值。' },
  ]);
}

{
  const segments: MessageSegment[] = [tool(0), { type: 'text', content: '我再尝试查询' }];
  const deferred = deferThinkingTextFragmentBeforeTool(segments, true, undefined);

  assert.equal(deferred, undefined);
  assert.deepEqual(segments, [tool(0), { type: 'text', content: '我再尝试查询' }]);
}

{
  const segments: MessageSegment[] = [{ type: 'text', content: '数' }];
  const deferred = deferThinkingTextFragmentBeforeTool(segments, true, undefined);

  assert.equal(deferred, undefined);
  assert.deepEqual(segments, [{ type: 'text', content: '数' }]);
}

{
  const original = { type: 'text' as const, content: '已有正文' };
  const segments: MessageSegment[] = [original];
  appendStreamTextSegment(segments, '继续', undefined);

  assert.notEqual(segments[0], original);
  assert.equal(segments[0]?.content, '已有正文继续');
}

{
  // Real production ordering: content "已" → late thinking "." → content "为你完成".
  // The late reasoning punctuation belongs to the prior thinking block and must
  // not split the answer around the completed tool run.
  const segments: MessageSegment[] = [
    tool(0),
    { type: 'thinking', content: 'Let me compose the answer' },
    { type: 'text', content: '已' },
  ];
  const merged = appendThinkingContentBeforeTrailingText(segments, '.');
  appendStreamTextSegment(segments, '为你完成', undefined);

  assert.equal(merged, true);
  assert.deepEqual(segments, [
    tool(0),
    { type: 'thinking', content: 'Let me compose the answer.' },
    { type: 'text', content: '已为你完成' },
  ]);
}

{
  const segments: MessageSegment[] = [{ type: 'text', content: '正文' }];
  const merged = appendThinkingContentBeforeTrailingText(segments, '迟到思考');

  assert.equal(merged, false);
  assert.deepEqual(segments, [{ type: 'text', content: '正文' }]);
}

{
  const segments: MessageSegment[] = [tool(0), { type: 'text', content: '数' }];
  let deferred = deferThinkingTextFragmentBeforeTool(segments, true, undefined);
  segments.push(tool(1));
  deferred = restoreDeferredThinkingTextFragment(segments, deferred);

  assert.equal(deferred, undefined);
  assert.deepEqual(segments, [tool(0), { type: 'text', content: '数' }, tool(1)]);
}

{
  // A Write call is not valid JSON until its final delta, but escaped newlines
  // must already render as real multi-line code in the folded tool preview.
  const partial = '{"file_path":"src/demo.ts","content":"const a = 1;\\nconst b = 2;\\n';
  assert.deepEqual(extractCodeFromStreamingArgs('Write', partial), {
    code: 'const a = 1;\nconst b = 2;\n',
    language: 'typescript',
  });
}

{
  const partial = '{"command":"printf \\"first\\\\nsecond\\"';
  assert.deepEqual(extractCodeFromStreamingArgs('bash', partial), {
    code: 'printf "first\\nsecond"',
    language: 'bash',
  });
}

{
  // When there are more tool calls than persisted thinking blocks, every
  // unmatched tool still happened before the final answer and must not be
  // appended below it after refresh.
  const toolCalls = [0, 1, 2].map((i) => ({
    id: `tool-${i}`,
    name: 'demo',
    status: 'success' as const,
  }));
  const { segments, cleanContent } = buildHistorySegments(
    '<think>分析任务</think>最终回答',
    toolCalls,
  );

  assert.deepEqual(segments?.map((segment) => segment.type), [
    'thinking',
    'tool',
    'tool',
    'tool',
    'text',
  ]);
  assert.equal(cleanContent, '最终回答');
}

{
  // Without persisted offsets (legacy history), there is no reliable cut
  // point, so the fallback keeps the visible answer as one Markdown block
  // instead of guessing splits around every tool call.
  const phases = [
    '我来',
    '帮你查找最新的自进化相关文章。首先让我确认一下当前可访问的项目。',
    '当前',
    '只有一个项目「agent harness」。让我在这个项目里查找自进化相关的最新文章。',
    '项目里有',
    '1276 篇论文。让我用多种方式检索自进化相关内容。',
  ];
  const content = phases.join('');
  const toolCalls = phases.slice(0, -1).map((_, i) => ({
    id: `tool-${i}`,
    name: 'demo',
    status: 'success' as const,
  }));
  const { segments, cleanContent } = buildHistorySegments(content, toolCalls);
  assert.equal(cleanContent, content);
  assert.deepEqual(segments?.map((segment) => segment.type), [
    'tool', 'tool', 'tool', 'tool', 'tool', 'text',
  ]);
  assert.equal(segments?.at(-1)?.content, content);
}

{
  // With persisted contentOffset, history restores the original streaming
  // interleave: text ↔ tool cards in chronological order, thinking blocks
  // split inside each slice.
  const content = '<think>先想</think>先查一下。<think>再想</think>查到了，继续。最终结论。';
  const off0 = content.indexOf('<think>再想');
  const off1 = content.indexOf('最终结论。');
  const toolCalls = [
    { id: 'tool-0', name: 'demo', status: 'success' as const, contentOffset: off0 },
    { id: 'tool-1', name: 'demo', status: 'success' as const, contentOffset: off1 },
  ];
  const { segments, cleanContent } = buildHistorySegments(content, toolCalls);
  assert.deepEqual(segments, [
    { type: 'thinking', content: '先想' },
    { type: 'text', content: '先查一下。' },
    tool(0),
    { type: 'thinking', content: '再想' },
    { type: 'text', content: '查到了，继续。' },
    tool(1),
    { type: 'text', content: '最终结论。' },
  ]);
  assert.equal(cleanContent, '最终结论。');
}

{
  // Offset path mirrors the live defer rule: a single Han character stranded
  // right before a tool card is merged into the narration after it, so the
  // refreshed history matches what the live stream rendered.
  const content = '第一步完成。数仓确认无误，输出结果。';
  const toolCalls = [
    { id: 'tool-0', name: 'demo', status: 'success' as const, contentOffset: '第一步完成。'.length },
    { id: 'tool-1', name: 'demo', status: 'success' as const, contentOffset: '第一步完成。数'.length },
  ];
  const { segments, cleanContent } = buildHistorySegments(content, toolCalls);
  assert.deepEqual(segments, [
    { type: 'text', content: '第一步完成。' },
    tool(0),
    tool(1),
    { type: 'text', content: '数仓确认无误，输出结果。' },
  ]);
  assert.equal(cleanContent, '数仓确认无误，输出结果。');
}

{
  // Real-world corruption from structured reasoning: the tail of a thinking
  // sentence ("。") arrives after the first answer token and was persisted as
  // `答<think>。</think>案` — a think block puncturing the visible sentence.
  // History rebuild must mirror the live merge rule: fold the late tail into
  // the previous thinking block and rejoin the sentence seamlessly.
  const content = '<think>整理今日资讯</think>内部<think>。</think>产业资讯数据源今日暂时无法返回。';
  const toolCalls = [
    { id: 'tool-0', name: 'demo', status: 'success' as const, contentOffset: 0 },
  ];
  const { segments, cleanContent } = buildHistorySegments(content, toolCalls);
  assert.deepEqual(segments, [
    tool(0),
    { type: 'thinking', content: '整理今日资讯。' },
    { type: 'text', content: '内部产业资讯数据源今日暂时无法返回。' },
  ]);
  assert.equal(cleanContent, '内部产业资讯数据源今日暂时无法返回。');
}

{
  // Artifact pseudo-cards (appended from metadata.artifacts without offsets)
  // must not knock the whole message back to the stacked fallback — they sort
  // to the end while real tool calls keep their recorded interleave.
  const content = '先查询。查询完成，结论如下。';
  const toolCalls = [
    { id: 'tool-0', name: 'demo', status: 'success' as const, contentOffset: '先查询。'.length },
    { id: 'artifact_f1', name: '附件', status: 'success' as const },
  ];
  const { segments, cleanContent } = buildHistorySegments(content, toolCalls);
  // 工具/附件卡不允许落在正文末尾之后：排尾的附件卡挪到最终答案上方
  assert.deepEqual(segments, [
    { type: 'text', content: '先查询。' },
    tool(0),
    tool(1),
    { type: 'text', content: '查询完成，结论如下。' },
  ]);
  assert.equal(cleanContent, '查询完成，结论如下。');
}

{
  // Visible narration emitted between reasoning rounds must survive history
  // cleanup, while still rendering as one answer block.
  const toolCalls = [0, 1].map((i) => ({
    id: `thinking-tool-${i}`,
    name: 'demo',
    status: 'success' as const,
  }));
  const { segments, cleanContent } = buildHistorySegments(
    '第一段<think>思考一</think>第二段<think>思考二</think>第三段',
    toolCalls,
  );

  assert.equal(cleanContent, '第一段第二段第三段');
  assert.deepEqual(segments, [
    { type: 'thinking', content: '思考一' },
    tool(0),
    { type: 'thinking', content: '思考二' },
    tool(1),
    { type: 'text', content: '第一段第二段第三段' },
  ]);
}

{
  // Inline-reasoning providers may omit the opening tag. Everything before
  // the orphan closing tag is reasoning; only the suffix is visible body.
  const { segments, cleanContent } = buildHistorySegments(
    '先分析用户问题，再决定调用工具</think>这是最终回答。',
    [{ id: 'inline-tool', name: 'demo', status: 'success' }],
  );

  assert.equal(cleanContent, '这是最终回答。');
  assert.deepEqual(segments, [
    { type: 'thinking', content: '先分析用户问题，再决定调用工具' },
    tool(0),
    { type: 'text', content: '这是最终回答。' },
  ]);
}

{
  // Structured reasoning arrives through a separate reasoning field/SSE
  // event and is persisted in a paired <think> block by the backend.
  const { segments, cleanContent } = buildHistorySegments(
    '<think>结构化 reasoning 字段里的内容</think>这是结构化模型的正文。',
    [{ id: 'structured-tool', name: 'demo', status: 'success' }],
  );

  assert.equal(cleanContent, '这是结构化模型的正文。');
  assert.deepEqual(segments, [
    { type: 'thinking', content: '结构化 reasoning 字段里的内容' },
    tool(0),
    { type: 'text', content: '这是结构化模型的正文。' },
  ]);
}

console.log('chat stream segment tests passed');
