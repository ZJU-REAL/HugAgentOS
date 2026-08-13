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
  // Persisted offsets are not a valid rendering coordinate: backend content
  // includes reasoning markup and the live UI may merge streamed fragments.
  // History must keep the visible answer as one Markdown block instead of
  // splitting it around every tool call.
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
