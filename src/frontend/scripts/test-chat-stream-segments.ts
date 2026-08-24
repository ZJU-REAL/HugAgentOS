import assert from 'node:assert/strict';

import type { MessageSegment, SubagentStep } from '../src/types';
import {
  appendStreamTextSegment,
  appendSubagentStepDelta,
  appendThinkingContentBeforeTrailingText,
  deferThinkingTextFragmentBeforeTool,
  restoreDeferredThinkingTextFragment,
} from '../src/utils/streamSegments';
import { extractCodeFromStreamingArgs } from '../src/utils/codeExecParser';
import { buildHistorySegments } from '../src/utils/segments';
import { getToolRunInitialOpen } from '../src/utils/toolRunState';
import { refreshTargetForTool } from '../src/utils/toolRefresh';
import { parseAppliedQueueHandoff, parseQueuedRunHandoff } from '../src/utils/streamHandoff';

function tool(toolIndex: number): MessageSegment {
  return { type: 'tool', toolIndex };
}

{
  assert.deepEqual(parseQueuedRunHandoff({
    type: 'queued_run_started',
    run_id: 'run-child',
    message_id: 'msg-assistant',
    user_message_id: 'msg-user',
    message: '继续处理下一件事',
    queue_id: 'queue-1',
    steer_id: 'steer-1',
    delivery_mode: 'follow_up',
  }), {
    runId: 'run-child',
    messageId: 'msg-assistant',
    userMessageId: 'msg-user',
    message: '继续处理下一件事',
    queueId: 'queue-1',
    steerId: 'steer-1',
    deliveryMode: 'follow_up',
  });
  assert.equal(parseQueuedRunHandoff({
    run_id: 'run-child',
    delivery_mode: 'follow_up',
  }), undefined);
  assert.equal(parseAppliedQueueHandoff({ status: 'accepted' }), undefined);
  assert.equal(parseAppliedQueueHandoff({
    status: 'applied',
    applied_run_id: 'run-child',
    applied_run_message_id: 'msg-assistant',
    applied_user_message_id: 'msg-user',
    message: '继续处理下一件事',
    queue_id: 'queue-1',
    steer_id: 'steer-1',
    delivery_mode: 'next_run',
  })?.runId, 'run-child');
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

{
  // 子智能体子步骤：结构化 reasoning 的收尾增量在正文首 token 之后才到达
  // （线上实录形态：thinking " more" → content "Let" → thinking " searches." → content …）。
  // 迟到的思考尾并回前一个思考块，正文保持一整段，不被切成碎片交错。
  const steps: SubagentStep[] = [];
  appendSubagentStepDelta(steps, 'thinking', '继续检索剩余企业。');
  appendSubagentStepDelta(steps, 'thinking', ' more');
  appendSubagentStepDelta(steps, 'content', 'Let');
  appendSubagentStepDelta(steps, 'thinking', ' searches.');
  appendSubagentStepDelta(steps, 'content', ' me continue with more searches for');
  appendSubagentStepDelta(steps, 'content', ' remaining');

  assert.deepEqual(steps, [
    { kind: 'thinking', text: '继续检索剩余企业。 more searches.' },
    { kind: 'content', text: 'Let me continue with more searches for remaining' },
  ]);
}

{
  // 边界：工具步骤之后的思考属于新一轮，不并回上一轮思考块；
  // 没有前置思考块时（正文在先）也不能吞掉这条思考。
  const afterTool: SubagentStep[] = [
    { kind: 'thinking', text: '上一轮思考' },
    { kind: 'tool', toolId: 't1', name: 'internet_search', status: 'success' },
  ];
  appendSubagentStepDelta(afterTool, 'thinking', '新一轮思考');
  assert.deepEqual(afterTool, [
    { kind: 'thinking', text: '上一轮思考' },
    { kind: 'tool', toolId: 't1', name: 'internet_search', status: 'success' },
    { kind: 'thinking', text: '新一轮思考' },
  ]);

  const contentFirst: SubagentStep[] = [{ kind: 'content', text: '正文在先' }];
  appendSubagentStepDelta(contentFirst, 'thinking', '随后的思考');
  assert.deepEqual(contentFirst, [
    { kind: 'content', text: '正文在先' },
    { kind: 'thinking', text: '随后的思考' },
  ]);
}

{
  // 管理类插件写操作 → 必须刷"持有那份列表的" store。三份列表来自三个不同接口，
  // 刷错不会报错、只会静默无效——这张表已经错过两次，故在此钉住。
  const AGENT_WRITES = ['create_agent', 'edit_agent', 'delete_agent', 'install_market_agent'];
  const PLUGIN_WRITES = ['install_plugin', 'uninstall_plugin', 'import_plugin', 'set_plugin_enabled'];
  const SKILL_WRITES = ['register_skill', 'install_from_marketplace', 'delete_skill', 'edit_skill'];
  for (const n of AGENT_WRITES) assert.equal(refreshTargetForTool(n), 'agents', n);
  for (const n of PLUGIN_WRITES) assert.equal(refreshTargetForTool(n), 'plugins', n);
  for (const n of SKILL_WRITES) assert.equal(refreshTargetForTool(n), 'catalog', n);

  // 只读动词不该触发任何重拉。
  for (const n of [
    'search_agent_market', 'list_my_agents', 'list_bindable_capabilities',
    'search_plugin_market', 'list_my_plugins', 'get_plugin_info',
    'search_marketplace', 'list_my_skills', 'submit_agent_to_market', 'submit_to_marketplace',
  ]) assert.equal(refreshTargetForTool(n), undefined, n);

  // 精确匹配：uninstall_plugin 不能靠"包含 install_plugin"这种巧合被覆盖。
  assert.equal(refreshTargetForTool('uninstall_plugin'), 'plugins');
  assert.equal(refreshTargetForTool('totally_unknown_tool'), undefined);
}

console.log('chat stream segment tests passed');
