// The interactive app end to end: real InteractiveApp + pi-tui, a recording terminal, the scripted FakeEngine.
import assert from 'node:assert/strict';
import test from 'node:test';
import { InteractiveApp } from '../dist/tui/app.js';
import { FakeEngine } from './fixtures/tui-engine.mjs';

const strip = (s) => s.replace(/\x1b\[[0-9;]*m/g, '').replace(/\x1b\][^\x07]*\x07/g, '').replace(/\x1b_pi:c\x07/g, '');
const tick = (ms = 15) => new Promise((resolve) => setTimeout(resolve, ms));

const KEY = { enter: '\r', esc: '\x1b', altEnter: '\x1b\r', down: '\x1b[B', ctrlC: '\x03', ctrlD: '\x04', ctrlT: '\x14' };

class RecordingTerminal {
  output = '';
  started = false;
  start(onInput, onResize) { this.onInput = onInput; this.onResize = onResize; this.started = true; }
  stop() { this.started = false; }
  async drainInput() {}
  write(data) { this.output += data; }
  get columns() { return 90; }
  get rows() { return 40; }
  get kittyProtocolActive() { return false; }
  moveBy() {}
  hideCursor() {}
  showCursor() {}
  clearLine() {}
  clearFromCursor() {}
  clearScreen() {}
  setTitle() {}
  setProgress() {}
}

function setup({ mode = 'local', chatId, speed = 0, prompt, pickSession } = {}) {
  const terminal = new RecordingTerminal();
  const engine = new FakeEngine(mode, { speed, cwd: process.cwd() });
  const created = [];
  const app = new InteractiveApp({
    engine,
    createEngine: async (m) => { const e = new FakeEngine(m, { speed: 0 }); created.push(e); return e; },
    cwd: process.cwd(),
    chatId,
    prompt,
    pickSession,
    terminal,
  });
  const done = app.start();
  const screen = () => app.tui.render(90).map(strip).join('\n');
  const type = async (text, settle = 15) => {
    for (const ch of text) terminal.onInput(ch);
    await tick(settle);
  };
  const press = async (key, settle = 15) => { terminal.onInput(key); await tick(settle); };
  const until = async (predicate, what, limitMs = 2000) => {
    for (let i = 0; i < limitMs / 10; i++) {
      if (predicate()) return;
      await tick(10);
    }
    assert.fail(`timed out waiting for ${what}\n${screen()}`);
  };
  const calls = (method, e = engine) => e.calls.filter((c) => c.method === method);
  return { app, engine, terminal, done, screen, type, press, until, calls, created };
}

async function quit(ctx) {
  await ctx.press(KEY.ctrlC, 5);
  await ctx.press(KEY.ctrlC, 5);
  assert.equal(await ctx.done, 0);
}

test('send → tools → inline approval → answer; the session is created lazily', async () => {
  const ctx = setup();
  assert.match(ctx.screen(), /local · /);
  await ctx.type('看看项目');
  await ctx.press(KEY.enter);
  await ctx.until(() => /需要确认 · 写入文件 NOTES\.md/.test(ctx.screen()), 'the approval prompt');
  assert.equal(ctx.calls('newSession').length, 1);
  assert.match(ctx.screen(), /✓ list_dir · \. · 4 行/);
  assert.match(ctx.screen(), /✗ bash · npm test · exit code 1/);
  assert.match(ctx.screen(), /等待确认/);
  await ctx.press(KEY.down); // 本会话内都允许
  await ctx.press(KEY.enter);
  await ctx.until(() => /空闲/.test(ctx.screen()), 'the run to end');
  const [confirm] = ctx.calls('confirm');
  assert.equal(confirm.args[2], 'allow_session');
  const out = ctx.screen();
  assert.match(out, /✓ 本会话内已允许 · 写入文件 NOTES\.md/);
  assert.match(out, /✓ write_file · NOTES\.md/);
  assert.match(out, /项目是一个 TypeScript 包/);
  assert.match(out, /ctx 19%/);
  await quit(ctx);
  assert.ok(ctx.engine.closed, 'engine closed on exit');
  assert.equal(ctx.terminal.started, false, 'terminal restored');
});

test('question prompt answers go to engine.answer', async () => {
  const ctx = setup();
  await ctx.type('有歧义就问我 question');
  await ctx.press(KEY.enter);
  await ctx.until(() => /新服务用哪个数据库/.test(ctx.screen()), 'the question');
  await ctx.press(KEY.enter); // PostgreSQL (recommended, preselected)
  await ctx.until(() => /需要哪些附加功能/.test(ctx.screen()), 'question 2');
  await ctx.press(' ');
  await ctx.press(KEY.enter);
  await ctx.until(() => /空闲/.test(ctx.screen()), 'the run to end');
  const [answer] = ctx.calls('answer');
  assert.deepEqual(answer.args[2], [{ id: 'db', selected: ['pg'] }, { id: 'features', selected: ['auth'] }]);
  assert.match(ctx.screen(), /已回答：PostgreSQL \| 登录鉴权/);
  await quit(ctx);
});

test('while streaming: Enter steers, Alt+Enter queues a follow-up that runs next', async () => {
  const ctx = setup({ speed: 1 });
  await ctx.type('写长一点 long');
  await ctx.press(KEY.enter);
  await ctx.until(() => /长回答/.test(ctx.screen()), 'the answer to stream');
  await ctx.type('改成英文', 0);
  await ctx.press(KEY.enter, 0);
  await ctx.type('再总结 long', 0);
  await ctx.press(KEY.altEnter, 30);
  assert.match(ctx.screen(), /排队: 再总结 long/);
  const steers = ctx.calls('steer').map((c) => [c.args[1], c.args[2]]);
  assert.deepEqual(steers, [['改成英文', 'steer'], ['再总结 long', 'followUp']]);
  await ctx.until(() => ctx.calls('resume').length === 1, 'the follow-up run');
  await ctx.until(() => !ctx.app.run, 'the follow-up run to finish');
  const out = ctx.screen();
  assert.match(out, /插话 改成英文/);
  assert.match(out, /排队 再总结 long/);
  assert.doesNotMatch(out, /排队: /, 'nothing left pending');
  await quit(ctx);
});

test('approval: Esc on the prompt denies', async () => {
  const ctx = setup();
  await ctx.type('再来一次');
  await ctx.press(KEY.enter);
  await ctx.until(() => /需要确认/.test(ctx.screen()), 'the approval prompt');
  await ctx.press(KEY.esc);
  assert.equal(ctx.calls('confirm').at(-1).args[2], 'deny');
  await ctx.until(() => ctx.app.run === undefined, 'the denied run to finish');
  assert.match(ctx.screen(), /✗ write_file · NOTES\.md · 用户拒绝了写入/);
  await quit(ctx);
});

test('Esc cancels a running turn and puts queued input back into the editor', async () => {
  const ctx = setup();
  await ctx.type('有歧义就问我 question'); // waits on a question, so the run stays open
  await ctx.press(KEY.enter);
  await ctx.until(() => /新服务用哪个数据库/.test(ctx.screen()), 'the question');
  await ctx.press(KEY.esc); // on the question: skip all, the run continues
  await ctx.until(() => !/新服务用哪个数据库/.test(ctx.screen()), 'the prompt to close');
  await ctx.until(() => ctx.app.run === undefined, 'run end');
  // A run waiting on a question, cancelled from the editor with Esc.
  await ctx.type('问我 question');
  await ctx.press(KEY.enter);
  await ctx.until(() => /新服务用哪个数据库/.test(ctx.screen()), 'the question again');
  ctx.app.tui.setFocus(ctx.app.editor); // the user leaves the prompt (as after ctrl+c in pi)
  await ctx.type('等下再说');
  await ctx.press(KEY.altEnter);
  await ctx.press(KEY.esc);
  await ctx.until(() => ctx.app.run === undefined, 'cancel');
  assert.equal(ctx.calls('cancel').length, 1);
  assert.match(ctx.screen(), /◼ 已中断/);
  assert.doesNotMatch(ctx.screen(), /已被用户中断/);
  assert.equal(ctx.app.editor.getText(), '等下再说', 'queued text restored');
  await quit(ctx);
});

test('/mode cloud swaps the engine, starts a session there and disables ! and @', async () => {
  const ctx = setup();
  await ctx.type('/mode cloud');
  await ctx.press(KEY.enter, 40);
  await ctx.until(() => /cloud · 无本地文件访问/.test(ctx.screen()), 'the cloud footer');
  const [cloud] = ctx.created;
  assert.ok(cloud.connected);
  assert.ok(ctx.engine.closed, 'old engine closed');
  assert.equal(ctx.calls('newSession', cloud).length, 1);
  assert.match(ctx.screen(), /── cloud · agent\.example\.com · 无本地文件访问/);
  await ctx.type('!ls');
  await ctx.press(KEY.enter);
  assert.match(ctx.screen(), /! 命令只在 local 模式可用/);
  await quit(ctx);
});

test('/model picks a model that the next send carries', async () => {
  const ctx = setup();
  await ctx.type('/model');
  await ctx.press(KEY.enter, 30);
  await ctx.until(() => /选择模型/.test(ctx.screen()), 'the model list');
  await ctx.press(KEY.down);
  await ctx.press(KEY.enter);
  assert.match(ctx.screen(), /DeepSeek Reasoner · 审批:按需/);
  await ctx.type('长 long');
  await ctx.press(KEY.enter);
  await ctx.until(() => !ctx.app.run && ctx.calls('send').length === 1, 'the send');
  assert.deepEqual(ctx.calls('send')[0].args[2], { providerId: 'deepseek', modelName: 'deepseek-reasoner' });
  await quit(ctx);
});

test('!command output is shown and prepended to the next message', async () => {
  const ctx = setup();
  await ctx.type('!echo 你好-from-shell');
  await ctx.press(KEY.enter);
  await ctx.until(() => /✓/.test(ctx.screen()) && /你好-from-shell\s*$/m.test(ctx.screen()), 'the command output');
  await ctx.type('解释一下 long');
  await ctx.press(KEY.enter);
  await ctx.until(() => ctx.calls('send').length === 1 && !ctx.app.run, 'the send');
  const message = ctx.calls('send')[0].args[1];
  assert.match(message, /^我在终端里执行了 `echo 你好-from-shell`（退出码 0）：\n```\n你好-from-shell\n```\n\n解释一下 long$/);
  await quit(ctx);
});

test('starting with a chat id renders its history; /resume and /new work; Ctrl+D quits', async () => {
  const ctx = setup({ chatId: 'chat-demo' });
  await ctx.until(() => /Safari 15 之前不支持/.test(ctx.screen()), 'history');
  assert.match(ctx.screen(), /✓ read_file · src\/pages\/Login\.tsx · 41 行/);
  await ctx.type('/new');
  await ctx.press(KEY.enter);
  assert.match(ctx.screen(), /── 新会话/);
  assert.equal(ctx.app.chatId, undefined);
  await ctx.type('/resume');
  await ctx.press(KEY.enter, 30);
  await ctx.until(() => /恢复会话/.test(ctx.screen()), 'the session list');
  await ctx.press(KEY.enter);
  await ctx.until(() => ctx.app.chatId === 'chat-demo', 'resume');
  await ctx.press(KEY.ctrlT);
  assert.match(ctx.screen(), /先看看登录组件和构建配置/, 'ctrl+t expands thinking');
  await ctx.press(KEY.ctrlD);
  assert.equal(await ctx.done, 0);
});

test('unknown slash commands are not sent as chat', async () => {
  const ctx = setup();
  await ctx.type('/nope');
  await ctx.press(KEY.esc); // close autocomplete if any
  await ctx.press(KEY.enter);
  assert.equal(ctx.calls('send').length, 0);
  assert.match(ctx.screen(), /未知命令 \/nope/);
  await quit(ctx);
});

test('steer text over 10000 chars is not steered; it is sent as the next message', async () => {
  const ctx = setup({ speed: 1 });
  await ctx.type('写长一点 long');
  await ctx.press(KEY.enter);
  await ctx.until(() => /长回答/.test(ctx.screen()), 'the answer to stream');
  const long = 'long ' + 'x'.repeat(10_000); // "long" picks a script without an approval
  ctx.terminal.onInput(`\x1b[200~${long}\x1b[201~`); // bracketed paste
  await tick(15);
  await ctx.press(KEY.enter, 30);
  assert.equal(ctx.calls('steer').length, 0);
  assert.match(ctx.screen(), /下一条: long x{10}/);
  assert.match(ctx.screen(), /超过 10000 字不能插话/);
  await ctx.until(() => ctx.calls('send').length === 2 && !ctx.app.run, 'the long text to go out as a normal send', 10_000);
  assert.equal(ctx.calls('send')[1].args[1], long);
  await quit(ctx);
});

test('/compact says what the server did: folded tokens, or nothing old enough to fold', async () => {
  const ctx = setup({ chatId: 'chat-demo' });
  await ctx.until(() => /local · /.test(ctx.screen()), 'the app');
  ctx.engine.compactResult = { compacted: false, tokensBefore: 18018, tokensAfter: null };
  await ctx.type('/compact');
  await ctx.press(KEY.enter);
  await ctx.until(() => /无需压缩/.test(ctx.screen()), 'the nothing-to-compact notice');
  assert.match(ctx.screen(), /无需压缩 · 当前约 18k tokens/);
  assert.doesNotMatch(ctx.screen(), /已压缩上下文/);
  ctx.engine.compactResult = { compacted: true, tokensBefore: 48210, tokensAfter: 6120 };
  await ctx.type('/compact');
  await ctx.press(KEY.enter);
  await ctx.until(() => /已压缩上下文/.test(ctx.screen()), 'the compacted notice');
  assert.match(ctx.screen(), /✓ 已压缩上下文 · 48k → 约 6\.1k tokens/);
  assert.equal(ctx.calls('compact').length, 2);
  await quit(ctx);
});

test('a slow /compact still repaints the terminal when it finishes', async () => {
  // Slower than the "正在压缩…" hint (which repaints once when it expires), as a real compaction is.
  const ctx = setup({ chatId: 'chat-demo', speed: 6 });
  await ctx.until(() => /local · /.test(ctx.screen()), 'the app');
  await ctx.type('/compact');
  await ctx.press(KEY.enter);
  const written = () => strip(ctx.terminal.output);
  await ctx.until(() => /已压缩上下文 · 48k → 约 6\.1k tokens/.test(written()), 'the notice on the terminal itself', 6000);
  await quit(ctx);
});

test('a prompt on the command line is sent as the first message', async () => {
  const ctx = setup({ prompt: '看看项目' });
  await ctx.until(() => ctx.calls('send').length === 1, 'the first send');
  assert.equal(ctx.calls('send')[0].args[1], '看看项目');
  // The scripted run stops at an approval, which holds the keyboard; deny it and let the run end.
  await ctx.until(() => /需要确认/.test(ctx.screen()), 'the approval prompt');
  await ctx.press(KEY.esc);
  await ctx.until(() => /空闲/.test(ctx.screen()), 'the run to end', 5000);
  await quit(ctx);
});

test('-r without an id opens the session picker', async () => {
  const ctx = setup({ pickSession: true });
  await ctx.until(() => /恢复会话/.test(ctx.screen()), 'the session picker');
  assert.equal(ctx.calls('listSessions').length, 1);
  await ctx.press(KEY.esc);
  await quit(ctx);
});
