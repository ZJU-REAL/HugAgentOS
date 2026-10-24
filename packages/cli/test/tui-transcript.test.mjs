// Transcript / footer / prompt rendering, driven by backend SSE events (no terminal needed).
import assert from 'node:assert/strict';
import test from 'node:test';
import chalk from 'chalk';
import { visibleWidth } from '@mariozechner/pi-tui';

// Colours on even when stdout is not a TTY; theme colours are built at import, so import after.
chalk.level = 3;
const { Transcript, ThinkSplitter } = await import('../dist/tui/transcript.js');
const { Footer } = await import('../dist/tui/footer.js');
const { contextPercent, summarizeArgs, resultSize, parseArgs } = await import('../dist/tui/format.js');
const { QuestionPrompt } = await import('../dist/tui/prompts/question.js');
const { createConfirmPrompt } = await import('../dist/tui/prompts/confirm.js');
const { parseQuestion, parseConfirm } = await import('../dist/tui/events.js');

const strip = (s) => s.replace(/\x1b\[[0-9;]*m/g, '').replace(/\x1b\][^\x07]*\x07/g, '');
const screen = (component, width = 80) => component.render(width).map(strip).join('\n');
const fresh = () => new Transcript({ expandThinking: false, expandTools: false });
const feed = (t, events) => events.flatMap((e) => t.apply(e));

test('content deltas stream into one Markdown block', () => {
  const t = fresh();
  feed(t, [{ type: 'content', delta: '## 标题\n\n第一' }, { type: 'content', delta: '段 **加粗**' }, { type: 'text', text: '。' }]);
  const out = screen(t.container);
  assert.match(out, /标题/);
  assert.match(out, /第一段 加粗。/);
  assert.equal(t.current.text, '## 标题\n\n第一段 **加粗**。');
});

test('content_replace swaps the answer text', () => {
  const t = fresh();
  feed(t, [{ type: 'content', delta: '旧答案' }, { type: 'content_replace', content: '新答案' }]);
  const out = screen(t.container);
  assert.doesNotMatch(out, /旧答案/);
  assert.match(out, /新答案/);
});

test('thinking collapses to one line and expands with the view toggle', () => {
  const t = fresh();
  feed(t, [{ type: 'thinking', delta: '先想一想' }, { type: 'thinking', delta: '再想一想' }, { type: 'content', delta: '答' }]);
  t.endTurn();
  let out = screen(t.container);
  assert.match(out, /思考 \d+ 秒/);
  assert.doesNotMatch(out, /先想一想/);
  t.view.expandThinking = true;
  out = screen(t.container);
  assert.match(out, /先想一想再想一想/);
});

test('inline <think> tags are split out even when cut across deltas', () => {
  const s = new ThinkSplitter();
  const parts = [...s.feed('<thi'), ...s.feed('nk>推理</th'), ...s.feed('ink>正文'), ...s.flush()];
  assert.deepEqual(parts.filter((p) => p.text), [{ kind: 'thinking', text: '推理' }, { kind: 'text', text: '正文' }]);
});

test('tool call is one quiet line: name · args · size · duration', () => {
  const t = fresh();
  feed(t, [
    { type: 'tool_call_start', id: 't1', name: 'read_file' },
    { type: 'tool_call_delta', id: 't1', arguments_delta: '{"path": "src/ma' },
  ]);
  assert.match(screen(t.container), /read_file · src\/ma/);
  feed(t, [
    { type: 'tool_call', id: 't1', name: 'read_file', input: { path: 'src/main.ts' } },
    { type: 'tool_progress', id: 't1', progress: 3, total: 10, message: '读取中' },
  ]);
  assert.match(screen(t.container), /read_file · src\/main\.ts · 3\/10 读取中/);
  feed(t, [{ type: 'tool_result', id: 't1', output: 'a\nb\nc', duration_ms: 1500 }]);
  const line = screen(t.container).split('\n').find((l) => l.includes('read_file'));
  assert.equal(line.trim(), '✓ read_file · src/main.ts · 3 行 · 1.5s');
});

test('tool errors render red with the error text', () => {
  const t = fresh();
  feed(t, [{ type: 'tool_call', id: 'b', name: 'bash', input: { command: 'npm test' } }, { type: 'tool_result', id: 'b', status: 'error', error: 'exit code 1' }]);
  const raw = t.container.render(80).find((l) => l.includes('bash'));
  assert.match(strip(raw), /✗ bash · npm test · exit code 1/);
  assert.ok(raw.includes('\x1b[38;2;204;102;102m'), 'error line is red');
});

test('subagent events nest under their parent tool', () => {
  const t = fresh();
  feed(t, [
    { type: 'tool_call', id: 'p', name: 'call_subagent', input: { task: '审查' } },
    { type: 'subagent_event', parent_tool_id: 'p', sub_type: 'tool_call', tool_id: 's1', tool_name: 'grep', input: { pattern: 'password' } },
    { type: 'subagent_event', parent_tool_id: 'p', sub_type: 'tool_result', tool_id: 's1', tool_name: 'grep', output: 'x' },
    { type: 'tool_result', id: 'p', subagent_name: '审查员', output: 'ok', duration_ms: 10 },
  ]);
  const out = screen(t.container);
  assert.match(out, /智能体 · 审查员 · 审查/);
  assert.match(out, /└ ✓ grep · password/);
});

test('approval, question and lifecycle events become signals', () => {
  const t = fresh();
  const signals = feed(t, [
    { type: 'run_started', run_id: 'r1' },
    { type: 'file_confirm', confirm_id: 'c1', op: 'write', logical_path: 'a.md', options: [{ id: 'o1', title: '方案一' }] },
    { type: 'file_confirm', confirm_id: 'c1', expired: true },
    { type: 'user_question', request_id: 'q1', questions: [{ id: 'x', question: '选哪个？', options: [{ id: 'a', label: 'A（推荐）' }] }] },
    { type: 'user_question_resolved', request_id: 'q1', outcome: 'answered' },
    { type: 'context_usage', used_tokens: 32000, context_window: 128000 },
    { type: 'plan_update', title: 'P', steps: [{ title: 's1', status: 'in_progress' }] },
    { type: 'queued_run_started', run_id: 'r2', message: '下一条', delivery_mode: 'follow_up' },
    { type: 'some_future_event', foo: 1 },
    { type: 'end' },
  ]);
  assert.deepEqual(signals.map((s) => s.type), ['run_started', 'confirm', 'confirm_expired', 'question', 'question_resolved', 'context', 'plan', 'queued_run', 'end']);
  assert.equal(signals[1].request.options[0].id, 'o1');
  assert.equal(signals[3].request.questions[0].options[0].label, 'A');
  assert.equal(signals[3].request.questions[0].options[0].recommended, true);
  assert.equal(signals[5].percent, 25);
});

test('steer_applied splits the reply; a call started before it still finishes in place', () => {
  const t = fresh();
  t.beginRun();
  feed(t, [{ type: 'tool_call', id: 'k', name: 'bash', input: { command: 'sleep 1' } }, { type: 'steer_applied', message: '换个方向' }, { type: 'content', delta: '好的' }]);
  let out = screen(t.container);
  assert.match(out, /插话 换个方向/);
  assert.ok(out.indexOf('bash') < out.indexOf('换个方向') && out.indexOf('换个方向') < out.indexOf('好的'));
  feed(t, [{ type: 'tool_result', id: 'k', output: 'done', duration_ms: 1000 }]);
  out = screen(t.container);
  assert.equal(out.match(/bash/g).length, 1, 'no duplicate tool line');
  assert.match(out, /✓ bash · sleep 1 · 4 B · 1.0s/);
});

test('errors are red lines, but not after the user cancelled', () => {
  const t = fresh();
  feed(t, [{ type: 'error', error: { message: '上游超时' } }]);
  assert.match(screen(t.container), /✗ 上游超时/);
  const c = fresh();
  c.beginRun();
  c.cancelling = true;
  feed(c, [{ type: 'error', error: '已被用户中断' }]);
  assert.doesNotMatch(screen(c.container), /已被用户中断/);
});

test('history renders as finished turns', () => {
  const t = fresh();
  t.renderHistory([
    { message_id: '1', role: 'user', content: '你好' },
    { message_id: '2', role: 'assistant', content: '<think>想</think>你好！', tool_calls: [{ id: 'x', name: 'ls', input: { path: '.' }, status: 'success', output: 'a' }] },
  ]);
  const out = screen(t.container);
  assert.match(out, /^ 你好\s*$/m);
  assert.match(out, /✓ ls · \./);
  assert.match(out, /你好！/);
  assert.equal(t.lastAnswer, '你好！');
});

test('stored history keeps text and tool calls interleaved as they streamed', () => {
  const t = fresh();
  t.renderHistory([
    { message_id: '1', role: 'user', content: '修 bug' },
    {
      message_id: '2', role: 'assistant', content: 'Look first.Found it.Fixed.',
      tool_calls: [
        { tool_id: 'a', tool_name: 'Read', tool_args: { file_path: 'calc.py' }, status: 'success', result: 'x', duration_ms: 300 },
        { tool_id: 'b', tool_name: 'Write', tool_args: { file_path: '../out.txt' }, status: 'error', result: { error: '用户拒绝了该本机操作' } },
      ],
      metadata: { segments: [
        { type: 'text', text: 'Look first.' }, { type: 'tool', index: 0 },
        { type: 'text', text: 'Found it.' }, { type: 'tool', index: 1 }, { type: 'text', text: 'Fixed.' },
      ] },
    },
  ]);
  const out = screen(t.container);
  assert.match(out, /✓ Read · calc\.py/);
  assert.match(out, /✗ Write · \.\.\/out\.txt/);
  assert.doesNotMatch(out, /✓ tool ·/);
  assert.doesNotMatch(out, /first\.Found/);
  const order = ['修 bug', 'Look first.', 'Read', 'Found it.', 'Write', 'Fixed.'].map((text) => out.indexOf(text));
  assert.deepEqual([...order].sort((a, b) => a - b), order, 'user, then text/tool/text/tool/text in order');
  assert.ok(order.every((index) => index >= 0));
});

test('Chinese text never overflows the width (east-asian widths)', () => {
  const t = fresh();
  t.addUser('请帮我把这个函数改成异步的，并且保留原来的错误处理逻辑，谢谢！'.repeat(3));
  feed(t, [
    { type: 'thinking', delta: '分析需求' },
    { type: 'tool_call', id: 'z', name: '读取文件', input: { path: '源代码/模块/中文路径名称很长很长很长很长.ts' } },
    { type: 'tool_result', id: 'z', output: '内容' },
    { type: 'content', delta: '这是一个很长的中文段落，包含 English words、`代码` 和 **加粗**，用来检查折行。'.repeat(4) + '\n\n| 列 | 说明 |\n|---|---|\n| 宽度 | 汉字占两列 |' },
  ]);
  for (const width of [20, 33, 47, 80, 120]) {
    for (const line of t.container.render(width)) assert.ok(visibleWidth(line) <= width, `width ${width}: ${strip(line)}`);
  }
  assert.equal(visibleWidth('汉字ab'), 6);
});

test('footer: cloud warning, permission, context percent', () => {
  const footer = new Footer({ info: { mode: 'cloud', label: 'cloud · agent.example.com', server: 'x', canAccessCwd: false }, model: 'deepseek-chat', permission: 'ask', contextPercent: 42.4 });
  const [where, stats] = footer.render(100).map(strip);
  assert.equal(where, 'cloud · 无本地文件访问 · agent.example.com');
  assert.match(stats, /^deepseek-chat · 审批:按需 · ctx 42%\s+空闲$/);
  assert.equal(visibleWidth(footer.render(100)[1]), 100);
  footer.update({ info: { mode: 'local', label: 'local · ~/repo', server: 'x', canAccessCwd: true }, runStartedAt: Date.now() });
  const lines = footer.render(30).map(strip);
  assert.equal(lines[0], 'local · ~/repo');
  for (const l of footer.render(30)) assert.ok(visibleWidth(l) <= 30);
});

test('format helpers', () => {
  assert.equal(summarizeArgs({ command: 'npm   test\n --watch' }), 'npm test --watch');
  assert.equal(summarizeArgs('{"path": "a/b.ts", "x"'), 'a/b.ts');
  assert.equal(resultSize('x\ny'), '2 行');
  assert.equal(resultSize({ ok: true }), '11 B');
  assert.equal(contextPercent({ used_tokens: 50, context_window: 200 }), 25);
  assert.equal(contextPercent({}), undefined);
  assert.deepEqual(parseArgs('{"a":1}'), { a: 1 });
});

test('question prompt: single pick, multi-select with custom text, esc skips the rest', () => {
  const request = parseQuestion({
    request_id: 'q', questions: [
      { id: 'db', question: '数据库？', options: [{ id: 'pg', label: 'PG' }, { id: 'my', label: 'MySQL（推荐）' }] },
      { id: 'f', question: '功能？', multi_select: true, options: [{ id: 'a', label: 'A' }, { id: 'b', label: 'B' }] },
      { id: 'z', question: '还有吗？', options: [{ id: 'n', label: '没有' }] },
    ],
  });
  let result;
  const p = new QuestionPrompt(request, (answers) => { result = answers; });
  assert.match(screen(p), /→ 2\. MySQL 推荐/, 'recommended option is preselected');
  p.handleInput('\r');
  assert.match(screen(p), /\(2\/3\) 功能？/);
  p.handleInput(' ');
  p.handleInput('\x1b[B');
  p.handleInput('\x1b[B');
  p.handleInput('\r'); // 自己输入…
  for (const ch of '中文补充') p.handleInput(ch);
  assert.match(screen(p), /中文补充/);
  p.handleInput('\r');
  p.handleInput('\x1b');
  assert.deepEqual(result, [
    { id: 'db', selected: ['my'] },
    { id: 'f', selected: ['a'], custom: '中文补充' },
    { id: 'z', selected: [], skipped: true },
  ]);
});

test('confirm prompt offers allow / allow_session / options / deny; esc denies', () => {
  const request = parseConfirm({ type: 'file_confirm', confirm_id: 'c', op: 'write', logical_path: 'a.md', options: [{ id: 'o', title: '覆盖' }] });
  const decisions = [];
  const p = createConfirmPrompt(request, (d) => decisions.push(d));
  const out = screen(p);
  for (const label of ['需要确认 · 写入文件 a.md', '允许', '本会话内都允许', '覆盖', '拒绝']) assert.ok(out.includes(label), label);
  p.handleInput('\x1b[B');
  p.handleInput('\x1b[B');
  p.handleInput('\r');
  p.handleInput('\x1b');
  assert.deepEqual(decisions, [{ decision: 'choice', optionId: 'o' }, { decision: 'deny' }]);
});
