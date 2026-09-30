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
import { refreshTargetForTool } from '../src/utils/toolRefresh';
import { parseAppliedQueueHandoff, parseQueuedRunHandoff } from '../src/utils/streamHandoff';
import { resolveSubagentParentIndex, resolveToolCardIndex, toolCardIndexById } from '../src/utils/toolMatching';

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
  assert.deepEqual(parseQueuedRunHandoff({
    type: 'queued_run_started',
    run_id: 'run-late-steer',
    message_id: 'msg-late-steer-assistant',
    user_message_id: 'msg-late-steer-user',
    message: '最后输出阶段追加的指令',
    queue_id: 'queue-late-steer',
    steer_id: 'steer-late',
    delivery_mode: 'steer',
  }), {
    runId: 'run-late-steer',
    messageId: 'msg-late-steer-assistant',
    userMessageId: 'msg-late-steer-user',
    message: '最后输出阶段追加的指令',
    queueId: 'queue-late-steer',
    steerId: 'steer-late',
    deliveryMode: 'steer',
  });
  assert.equal(parseAppliedQueueHandoff({
    status: 'applied',
    applied_run_id: 'run-source',
    applied_run_message_id: 'msg-source',
    applied_user_message_id: 'msg-user',
    message: '已注入当前运行',
    queue_id: 'queue-mid-run',
    steer_id: 'steer-mid-run',
    delivery_mode: 'steer',
  }, 'run-source'), undefined);
  assert.equal(parseAppliedQueueHandoff({
    status: 'applied',
    applied_run_id: 'run-late-steer',
    applied_run_message_id: 'msg-late-steer-assistant',
    applied_user_message_id: 'msg-late-steer-user',
    message: '最后输出阶段追加的指令',
    queue_id: 'queue-late-steer',
    steer_id: 'steer-late',
    delivery_mode: 'steer',
  }, 'run-source')?.runId, 'run-late-steer');
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
  assert.deepEqual(extractCodeFromStreamingArgs('Bash', partial), {
    code: 'printf "first\\nsecond"',
    language: 'bash',
  });
}

{
  // Legacy history has no persisted segment table. Its original process order
  // is unknowable, so history cleanup keeps only the visible body instead of
  // guessing how thinking blocks and tool cards were interleaved.
  const toolCalls = [0, 1, 2].map((i) => ({
    id: `tool-${i}`,
    name: 'demo',
    status: 'success' as const,
  }));
  const { segments, cleanContent } = buildHistorySegments(
    '<think>分析任务</think>最终回答',
    toolCalls,
  );

  assert.equal(segments, undefined);
  assert.equal(cleanContent, '最终回答');
}

{
  // Visible legacy narration remains intact even when old tool records exist.
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
  assert.equal(segments, undefined);
}

{
  // The persisted segment table is the single source of truth for the original
  // stream order. Text is inline; thinking and tools reference their columns.
  const content = '先查一下。查到了，继续。最终结论。';
  const toolCalls = [
    { id: 'tool-0', name: 'demo', status: 'success' as const },
    { id: 'tool-1', name: 'demo', status: 'success' as const },
  ];
  const thinking = [{ content: '先想' }, { content: '再想' }];
  const storedSegments = [
    { type: 'thinking' as const, index: 0 },
    { type: 'text' as const, text: '先查一下。' },
    { type: 'tool' as const, index: 0 },
    { type: 'thinking' as const, index: 1 },
    { type: 'text' as const, text: '查到了，继续。' },
    { type: 'tool' as const, index: 1 },
    { type: 'text' as const, text: '最终结论。' },
  ];
  const { segments, cleanContent } = buildHistorySegments(
    content,
    toolCalls,
    thinking,
    storedSegments,
  );
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
  // Segment-table replay mirrors the live defer rule: a single Han character stranded
  // right before a tool card is merged into the narration after it, so the
  // refreshed history matches what the live stream rendered.
  const content = '第一步完成。数仓确认无误，输出结果。';
  const toolCalls = [
    { id: 'tool-0', name: 'demo', status: 'success' as const },
    { id: 'tool-1', name: 'demo', status: 'success' as const },
  ];
  const storedSegments = [
    { type: 'text' as const, text: '第一步完成。' },
    { type: 'tool' as const, index: 0 },
    { type: 'text' as const, text: '数' },
    { type: 'tool' as const, index: 1 },
    { type: 'text' as const, text: '仓确认无误，输出结果。' },
  ];
  const { segments, cleanContent } = buildHistorySegments(
    content,
    toolCalls,
    undefined,
    storedSegments,
  );
  assert.deepEqual(segments, [
    { type: 'text', content: '第一步完成。' },
    tool(0),
    tool(1),
    { type: 'text', content: '数仓确认无误，输出结果。' },
  ]);
  assert.equal(cleanContent, '数仓确认无误，输出结果。');
}

{
  // Inline thinking markers inside a recorded text segment are still stripped,
  // so a provider cannot leak structured reasoning into visible history.
  const content = '内部产业资讯数据源今日暂时无法返回。';
  const toolCalls = [{ id: 'tool-0', name: 'demo', status: 'success' as const }];
  const thinking = [{ content: '整理今日资讯。' }];
  const storedSegments = [
    { type: 'thinking' as const, index: 0 },
    { type: 'tool' as const, index: 0 },
    {
      type: 'text' as const,
      text: '<think>不应显示的迟到尾巴</think>内部产业资讯数据源今日暂时无法返回。',
    },
  ];
  const { segments, cleanContent } = buildHistorySegments(
    content,
    toolCalls,
    thinking,
    storedSegments,
  );
  assert.deepEqual(segments, [
    { type: 'thinking', content: '整理今日资讯。' },
    tool(0),
    { type: 'thinking', content: '不应显示的迟到尾巴' },
    { type: 'text', content: '内部产业资讯数据源今日暂时无法返回。' },
  ]);
  assert.equal(cleanContent, '内部产业资讯数据源今日暂时无法返回。');
}

{
  // Artifact pseudo-cards appended after loading are absent from the stored
  // table. They still render above the final answer without disturbing the
  // recorded real-tool interleave.
  const content = '先查询。查询完成，结论如下。';
  const toolCalls = [
    { id: 'tool-0', name: 'demo', status: 'success' as const },
    { id: 'artifact_f1', name: '附件', status: 'success' as const },
  ];
  const storedSegments = [
    { type: 'text' as const, text: '先查询。' },
    { type: 'tool' as const, index: 0 },
    { type: 'text' as const, text: '查询完成，结论如下。' },
  ];
  const { segments, cleanContent } = buildHistorySegments(
    content,
    toolCalls,
    undefined,
    storedSegments,
  );
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
  // Multiple inline reasoning blocks in legacy history are stripped from the
  // body, but no process order is fabricated for them.
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
  assert.equal(segments, undefined);
}

{
  // Inline-reasoning providers may omit the opening tag. Everything before
  // the orphan closing tag is reasoning; only the suffix is visible body.
  const { segments, cleanContent } = buildHistorySegments(
    '先分析用户问题，再决定调用工具</think>这是最终回答。',
    [{ id: 'inline-tool', name: 'demo', status: 'success' }],
  );

  assert.equal(cleanContent, '这是最终回答。');
  assert.equal(segments, undefined);
}

{
  // Structured reasoning arrives through a separate reasoning field/SSE
  // event and is persisted in a paired <think> block by the backend.
  const { segments, cleanContent } = buildHistorySegments(
    '<think>结构化 reasoning 字段里的内容</think>这是结构化模型的正文。',
    [{ id: 'structured-tool', name: 'demo', status: 'success' }],
  );

  assert.equal(cleanContent, '这是结构化模型的正文。');
  assert.equal(segments, undefined);
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
  const PLUGIN_WRITES = ['install_plugin', 'uninstall_plugin', 'update_plugin', 'install_plugin_from_marketplace'];
  const SKILL_WRITES = ['install_skill', 'install_from_marketplace', 'uninstall_skill', 'update_skill', 'upload_skill_to_cloud'];
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

{
  // 工具结果只按 tool_call_id 归位。同名不是身份：并发跑 8 个 read_image 时同一个
  // 名字对应 8 张卡，按名字认领会把输出填进别人的卡里，还把真正那次调用藏起来。
  const parallel = [
    { id: 'call-a', name: 'read_image', status: 'running' },
    { id: 'call-b', name: 'read_image', status: 'running' },
    { id: 'call-c', name: 'read_image', status: 'running' },
  ];
  assert.equal(resolveToolCardIndex(parallel, 'call-c', 'read_image'), 2, '按 id 归位');
  assert.equal(resolveToolCardIndex(parallel, 'call-a', 'read_image'), 0, '按 id 归位');

  // 带了 id 却没有对应卡片 → 返回 -1，让调用方补建，绝不认领同名的在跑卡片。
  assert.equal(
    resolveToolCardIndex(parallel, 'call-zzz', 'read_image', { fallbackToAnyRunning: true }),
    -1,
    'id 认不到时不得按名字抢别人的卡',
  );

  // 完全没带 id 的事件才允许按名字兜底（取最后一张还在跑的同名卡）。
  assert.equal(resolveToolCardIndex(parallel, undefined, 'read_image'), 2);
  // 同名但已经收口的卡不该被重复认领。
  const settled = [
    { id: 'call-a', name: 'bash', status: 'success' },
    { id: 'call-b', name: 'bash', status: 'running' },
  ];
  assert.equal(resolveToolCardIndex(settled, undefined, 'bash'), 1);
  // 没 id 也没同名在跑：只有显式允许时才退到"任何还在跑的"。
  assert.equal(resolveToolCardIndex(settled, undefined, 'grep'), -1);
  assert.equal(resolveToolCardIndex(settled, undefined, 'grep', { fallbackToAnyRunning: true }), 1);
}

{
  // 子智能体事件同理：报了 parent_tool_id 就只认它。并行跑两个子智能体时，"最后一张
  // call_subagent 卡"同时对应两个还在跑的子智能体，按名字认领会把 A 的思考和子工具
  // 塞进 B 的卡里。
  const cards = [
    { id: 'sub-a', name: 'call_subagent', status: 'running' },
    { id: 'sub-b', name: 'call_subagent', status: 'running' },
  ];
  assert.equal(resolveSubagentParentIndex(cards, 'sub-a', 'call_subagent'), 0);
  assert.equal(resolveSubagentParentIndex(cards, 'sub-b', 'call_subagent'), 1);
  assert.equal(
    resolveSubagentParentIndex(cards, 'sub-zzz', 'call_subagent'),
    -1,
    'id 认不到时丢弃这条事件，不得贴到同名的另一个子智能体卡上',
  );

  // 批量作业的 job_progress 拿不到 CURRENT_TOOL_CALL_ID 时就是没带 id 的形状，
  // 它除了 run_job 这个名字没有别的线索——这条兜底必须留着。
  const jobCards = [
    { id: 'sub-a', name: 'call_subagent', status: 'running' },
    { id: 'job-1', name: 'run_job', status: 'running' },
  ];
  assert.equal(resolveSubagentParentIndex(jobCards, undefined, 'run_job'), 1);
}

{
  // 同一个 id 出现在两张卡上（按响应重新编号的网关下一轮会再发一次 call_0）：
  // 认还在跑的那张，别把上一轮已经跑完的结果盖掉。
  const reused = [
    { id: 'call_0', name: 'bash', status: 'success' },
    { id: 'call_0', name: 'bash', status: 'running' },
  ];
  assert.equal(toolCardIndexById(reused, 'call_0'), 1, '认未收口的那张卡');
  assert.equal(resolveToolCardIndex(reused, 'call_0', 'bash'), 1);

  // 全都收口了 → 退回第一张（同一个结果重复送达，内容一样）。
  const settled = [
    { id: 'call_0', name: 'bash', status: 'success' },
    { id: 'call_0', name: 'bash', status: 'error' },
  ];
  assert.equal(toolCardIndexById(settled, 'call_0'), 0);
  assert.equal(toolCardIndexById(settled, 'call_9'), -1);
}

console.log('chat stream segment tests passed');
