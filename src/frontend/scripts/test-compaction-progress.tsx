import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import { ToolRunShell } from '../src/components/tool/ToolRunShell';
import { groupExecutionRuns } from '../src/utils/executionRuns';
import { parseCompactionProgress, mergeCompactionProgress, upsertCompactionSegment, compactionLabel } from '../src/utils/compactionProgress';
import { t } from '../src/i18n';
import type { ChatMessage } from '../src/types';

const running = parseCompactionProgress({
  id: 'p1', chat_id: 'c1', run_id: 'r1', message_id: 'm1',
  status: 'running', phase: 'post_turn', started_at: Date.now(),
})!;
assert.ok(running);
assert.equal(parseCompactionProgress({ ...running, status: 'bogus' }), null);
const message: ChatMessage = {
  uid: 'u1', role: 'assistant', content: 'answer', messageId: 'm1', ts: 1,
  toolCalls: [{ id: 'tool1', name: 'search', status: 'success' }],
  segments: [{ type: 'tool', toolIndex: 0 }, { type: 'text', content: 'answer' }],
};
const [updated] = mergeCompactionProgress([message], 'c1', [running]);
assert.deepEqual(updated.segments!.map(s => s.type), ['tool', 'compaction', 'text']);
for (const visible of [true, false]) {
  const { runs } = groupExecutionRuns(updated, visible);
  assert.equal(runs.length, 1);
  assert.equal(runs[0].tools.length, 1);
  assert.deepEqual(runs[0].steps.map(s => s.kind), ['tool', 'compaction']);
  const html = renderToStaticMarkup(<ToolRunShell steps={runs[0].steps} isStreaming={false} />);
  assert.ok(html.includes(t('正在压缩上下文')));
  assert.ok(html.includes(t('{n} 个工具', { n: 1 })));
}
const completed = { ...running, status: 'completed' as const, ended_at: Date.now() };
const settled = upsertCompactionSegment(updated.segments!, completed);
assert.equal(settled.length, updated.segments!.length);
assert.equal(upsertCompactionSegment(settled, running)[1].compaction!.status, 'completed');
assert.equal(compactionLabel(completed), '上下文已压缩');
const failed = { ...running, status: 'failed' as const };
const [{ steps }] = groupExecutionRuns({ ...updated, segments: upsertCompactionSegment(updated.segments!, failed) }, false).runs;
assert.ok(renderToStaticMarkup(<ToolRunShell steps={steps} />).includes(t('上下文压缩失败')));
assert.deepEqual(mergeCompactionProgress([message], 'other', [running])[0], message);
assert.deepEqual(mergeCompactionProgress([message], 'c1', [{ ...running, message_id: 'other' }])[0], message);
console.log('compaction grouping, counting, replay and terminal state checks passed');

const expired = parseCompactionProgress({ ...running, expires_at: Date.now() - 1 })!;
assert.equal(expired.status, 'interrupted');
assert.equal(compactionLabel(expired), '压缩状态已过期');

assert.equal(upsertCompactionSegment(settled, expired)[1].compaction!.status, 'completed');

assert.equal(parseCompactionProgress({ ...running, status: ['running'] }), null);
