import assert from 'node:assert/strict';

import type { MessageSegment } from '../src/types';
import {
  appendStreamTextSegment,
  appendThinkingContentBeforeTrailingText,
  deferThinkingTextFragmentBeforeTool,
  restoreDeferredThinkingTextFragment,
} from '../src/utils/streamSegments';
import { extractCodeFromStreamingArgs } from '../src/utils/codeExecParser';

function tool(toolIndex: number): MessageSegment {
  return { type: 'tool', toolIndex };
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

console.log('chat stream segment tests passed');
