/**
 * The interactive session: pi's layout (transcript, queued messages, status, editor, footer),
 * driven by engine events. Owns run bookkeeping, steering, inline prompts and slash commands.
 */
import {
  Container, Loader, ProcessTerminal, Spacer, Text, TruncatedText, TUI,
  type Component, type Terminal,
} from '@mariozechner/pi-tui';
import type {
  ActiveRun, ChatEvent, DeliveryMode, Engine, Mode, PermissionMode, QuestionAnswer, RunOptions,
} from '../engine/engine.js';
import { STEER_LIMIT } from '../engine/base.js';
import { errorMessage } from '../engine/errors.js';
import { CLI_VERSION } from '../version.js';
import { AppEditor } from './app-editor.js';
import { BashBlock, runBash } from './bash.js';
import { copyToClipboard } from './clipboard.js';
import { AppAutocomplete, HELP_TEXT, HOTKEYS_TEXT, parseCommand } from './commands.js';
import type { ConfirmRequest, Plan, QuestionRequest } from './events.js';
import { Footer, PERMISSION_LABELS } from './footer.js';
import { formatTokens, oneLine } from './format.js';
import { PlanView } from './plan.js';
import { confirmTitle, createConfirmPrompt, type ConfirmDecision } from './prompts/confirm.js';
import { QuestionPrompt } from './prompts/question.js';
import { TitledSelect } from './prompts/select.js';
import { editorTheme, theme } from './theme.js';
import { Transcript, type Signal, type ViewState } from './transcript.js';

interface QueuedInput { text: string; mode: DeliveryMode; sent: boolean }

interface RunState {
  runId?: string;
  abort: AbortController;
  startedAt: number;
  cancelRequested: boolean;
  queued: QueuedInput[];
  handoff?: { runId: string; message: string };
  plan?: Plan;
}

type PendingPrompt =
  | { kind: 'confirm'; id: string; request: ConfirmRequest }
  | { kind: 'question'; id: string; request: QuestionRequest };

const DOUBLE_PRESS_MS = 1000;
const CANCEL_GRACE_MS = 3000;
/** Backend cap on steer messages (DESIGN.md §8.3); normal sends allow 100000. */


export interface AppOptions extends RunOptions {
  /** Injected in tests; defaults to the real terminal. */
  terminal?: Terminal;
}

export class InteractiveApp {
  readonly tui: TUI;
  readonly view: ViewState = { expandThinking: false, expandTools: false };
  readonly transcript = new Transcript(this.view);
  readonly editor: AppEditor;
  readonly footer: Footer;
  private header = new Container();
  private pending = new Container();
  private planBox = new Container();
  private status = new Container();
  private editorContainer = new Container();

  engine: Engine;
  chatId?: string;
  private model?: { providerId: string; modelName: string };
  private modelLabel?: string;
  run?: RunState;
  private prompts: PendingPrompt[] = [];
  private activePrompt?: { entry: PendingPrompt; component: Component };
  private loader?: Loader;
  private bash?: { block: BashBlock; kill: () => void };
  private bashContext: string[] = [];
  private nextMessages: string[] = [];
  private lastCtrlC = 0;
  private hintTimer?: NodeJS.Timeout;
  private switching = false;
  private shuttingDown = false;
  private exited!: (code: number) => void;
  readonly done: Promise<number>;

  constructor(private options: AppOptions) {
    this.engine = options.engine;
    this.chatId = options.chatId;
    this.model = options.model;
    this.done = new Promise((resolve) => { this.exited = resolve; });
    this.tui = new TUI(options.terminal ?? new ProcessTerminal());
    this.editor = new AppEditor(this.tui, editorTheme, { paddingX: 1 });
    this.editor.setAutocompleteProvider(new AppAutocomplete(options.cwd, () => this.engine.info.canAccessCwd));
    this.footer = new Footer({ info: this.engine.info, model: this.model?.modelName });
    this.editorContainer.addChild(this.editor);
    this.wireEditor();
  }

  // ── Lifecycle ────────────────────────────────────────────────────────────

  start(): Promise<number> {
    this.buildHeader();
    this.renderStatus();
    for (const c of [this.header, this.transcript.container, this.pending, this.planBox, this.status, this.editorContainer, this.footer]) {
      this.tui.addChild(c);
    }
    this.tui.setFocus(this.editor);
    this.tui.start();
    void this.init();
    return this.done;
  }

  private async init(): Promise<void> {
    void this.refreshEngineState();
    if (this.chatId) await this.loadSession(this.chatId);
    else if (this.options.pickSession) await this.runCommand('resume', ''); // `-r` with no id
    // `hugagent "…"`: the prompt on the command line is the first message.
    if (this.options.prompt?.trim()) await this.submit(this.options.prompt);
  }

  private buildHeader(): void {
    const v = CLI_VERSION;
    const logo = theme.bold(theme.accent('hugagent')) + (v ? theme.dim(` v${v}`) : '');
    const hints = ['esc 中断', 'ctrl+c 清空/退出', '/ 命令', '! 本机命令', 'alt+enter 排队', 'ctrl+t 思考', 'ctrl+o 工具']
      .map((h) => theme.dim(h)).join(theme.muted(' · '));
    this.header.addChild(new Spacer(1));
    this.header.addChild(new Text(`${logo}\n${hints}`, 1, 0));
  }

  /** Permission mode and model label for the footer; failures only leave the footer blank. */
  private async refreshEngineState(): Promise<void> {
    const engine = this.engine;
    const [permission, models] = await Promise.all([
      engine.getPermissionMode().catch(() => undefined),
      this.model ? Promise.resolve(undefined) : engine.listModels().catch(() => undefined),
    ]);
    if (engine !== this.engine) return;
    const current = models?.find((m) => m.current);
    this.footer.update({
      info: engine.info,
      permission,
      ...(this.model ? { model: this.modelLabel ?? this.model.modelName } : current ? { model: current.label || current.modelName } : {}),
    });
    this.tui.requestRender();
  }

  async shutdown(code = 0): Promise<void> {
    if (this.shuttingDown) return;
    this.shuttingDown = true;
    const run = this.run;
    if (run?.runId) await Promise.race([this.engine.cancel(run.runId).catch(() => {}), sleep(1500)]);
    run?.abort.abort();
    this.bash?.kill();
    this.loader?.stop();
    if (this.hintTimer) clearTimeout(this.hintTimer);
    try {
      await this.tui.terminal.drainInput?.(300);
    } catch { /* not every terminal drains */ }
    this.tui.stop();
    await Promise.race([this.engine.close().catch(() => {}), sleep(2000)]);
    this.exited(code);
  }

  /** Last-resort restore for uncaught errors: put the terminal back before anything is printed. */
  emergencyStop(): void {
    try {
      this.loader?.stop();
      this.tui.stop();
    } catch { /* already stopped */ }
  }

  // ── Editor wiring ────────────────────────────────────────────────────────

  private wireEditor(): void {
    this.editor.onSubmit = (text) => { void this.submit(text); };
    this.editor.onChange = (text) => {
      this.editor.borderColor = text.trimStart().startsWith('!') ? theme.bashMode : theme.borderMuted;
    };
    this.editor.actions = {
      escape: () => this.escape(),
      ctrlC: () => this.ctrlC(),
      exit: () => { void this.shutdown(); },
      toggleThinking: () => this.toggleView('expandThinking'),
      toggleTools: () => this.toggleView('expandTools'),
      followUp: (text) => {
        if (!this.run) return false;
        void this.queueInput(text, 'followUp');
        return true;
      },
    };
  }

  private toggleView(key: keyof ViewState): void {
    this.view[key] = !this.view[key];
    this.transcript.container.invalidate();
    this.flashHint(key === 'expandThinking'
      ? (this.view.expandThinking ? '思考已展开' : '思考已折叠')
      : (this.view.expandTools ? '工具详情已展开' : '工具详情已折叠'));
    this.tui.requestRender();
  }

  private escape(): void {
    if (this.bash) return this.bash.kill();
    if (this.run) return void this.cancelRun();
    if (this.editor.getText().startsWith('!')) {
      this.editor.setText('');
      this.editor.borderColor = theme.borderMuted;
      this.tui.requestRender();
    }
  }

  private ctrlC(): void {
    const now = Date.now();
    if (now - this.lastCtrlC < DOUBLE_PRESS_MS) {
      void this.shutdown();
      return;
    }
    this.lastCtrlC = now;
    this.editor.setText('');
    this.flashHint('再按一次 ctrl+c 退出');
    this.tui.requestRender();
  }

  /** A short dim line above the editor that clears itself. */
  private flashHint(text: string): void {
    if (this.hintTimer) clearTimeout(this.hintTimer);
    this.renderStatus(text);
    this.hintTimer = setTimeout(() => {
      this.hintTimer = undefined;
      this.renderStatus();
      this.tui.requestRender();
    }, DOUBLE_PRESS_MS + 200);
  }

  private renderStatus(hint?: string): void {
    this.status.clear();
    // The loader brings its own blank line; idle, keep one between transcript and editor like pi.
    this.status.addChild(this.loader ?? new Spacer(1));
    if (hint) this.status.addChild(new TruncatedText(theme.dim(hint), 1, 0));
  }

  // ── Input ────────────────────────────────────────────────────────────────

  private async submit(raw: string): Promise<void> {
    const text = raw.trim();
    if (!text) return;
    this.editor.addToHistory(text);
    const command = parseCommand(text);
    if (command) return this.runCommand(command.name, command.args);
    const unknown = /^\/([a-z][\w-]*)(\s|$)/i.exec(text);
    if (unknown) {
      this.editor.setText(text);
      this.flashHint(`未知命令 /${unknown[1]}，/help 查看全部命令`);
      this.tui.requestRender();
      return;
    }
    if (text.startsWith('!')) return this.bashCommand(text);
    if (this.run) return this.queueInput(text, 'steer');
    await this.sendMessage(text);
  }

  private async queueInput(text: string, mode: DeliveryMode): Promise<void> {
    const run = this.run;
    if (!run) return this.sendMessage(text);
    if (text.length > STEER_LIMIT) {
      // Too long to steer: it goes out as a normal message once this run ends.
      this.nextMessages.push(text);
      this.renderPending();
      this.flashHint(`超过 ${STEER_LIMIT} 字不能插话，会在本轮结束后作为普通消息发送`);
      this.tui.requestRender();
      return;
    }
    const item: QueuedInput = { text, mode, sent: false };
    run.queued.push(item);
    this.renderPending();
    if (run.runId) await this.deliver(run, item);
  }

  private async deliver(run: RunState, item: QueuedInput): Promise<void> {
    if (!run.runId || item.sent) return;
    item.sent = true;
    try {
      await this.engine.steer(run.runId, item.text, item.mode);
    } catch (error) {
      // The run may have just ended: fall back to sending it as the next message.
      run.queued = run.queued.filter((q) => q !== item);
      this.nextMessages.push(item.text);
      this.transcript.notice(`· 插话没送达（${errorMessage(error)}），会作为下一条消息发送`);
    }
    this.renderPending();
    this.tui.requestRender();
  }

  private renderPending(): void {
    this.pending.clear();
    const queued = this.run?.queued ?? [];
    if (!queued.length && !this.nextMessages.length) return;
    this.pending.addChild(new Spacer(1));
    for (const q of queued) {
      const label = q.mode === 'steer' ? '插话' : q.mode === 'followUp' ? '排队' : '下一条';
      this.pending.addChild(new TruncatedText(theme.dim(`${label}: ${oneLine(q.text, 200)}`), 1, 0));
    }
    for (const text of this.nextMessages) this.pending.addChild(new TruncatedText(theme.dim(`下一条: ${oneLine(text, 200)}`), 1, 0));
    this.pending.addChild(new TruncatedText(theme.dim('↳ esc 中断并把排队内容放回输入框'), 1, 0));
  }

  private async bashCommand(text: string): Promise<void> {
    const excluded = text.startsWith('!!');
    const command = text.slice(excluded ? 2 : 1).trim();
    if (!command) return;
    if (!this.engine.info.canAccessCwd) {
      this.transcript.notice(`! 命令只在 local 模式可用：当前是 ${this.engine.info.mode}，agent 访问不到本机文件。用 /mode local 切回本机。`, theme.warning);
      this.tui.requestRender();
      return;
    }
    if (this.bash) {
      this.flashHint('已有命令在运行，按 esc 先停止它');
      this.editor.setText(text);
      return;
    }
    const block = new BashBlock(command, excluded);
    this.transcript.add(block);
    const { child, done } = runBash(block, this.engine.info.workspace ?? this.options.cwd, () => this.tui.requestRender());
    this.bash = {
      block,
      kill: () => {
        block.killed = true;
        child.kill('SIGTERM');
      },
    };
    this.tui.requestRender();
    await done;
    this.bash = undefined;
    if (!excluded) this.bashContext.push(block.contextText());
    this.tui.requestRender();
  }

  // ── Runs ─────────────────────────────────────────────────────────────────

  private async ensureSession(text: string): Promise<string | undefined> {
    if (this.chatId) return this.chatId;
    try {
      const session = await this.engine.newSession(oneLine(text, 40));
      this.chatId = session.chatId;
      this.footer.update({ sessionTitle: undefined });
      return session.chatId;
    } catch (error) {
      this.transcript.error(`无法创建会话：${errorMessage(error)}`);
      this.tui.requestRender();
      return undefined;
    }
  }

  async sendMessage(text: string): Promise<void> {
    if (this.switching) {
      this.flashHint('正在切换执行位置，请稍候');
      this.editor.setText(text);
      return;
    }
    this.transcript.addUser(text);
    if (!this.engine.info.canAccessCwd && /(^|\s)@\S/.test(text)) {
      this.transcript.notice(`· 当前是 ${this.engine.info.mode}：agent 访问不到本机文件，@路径 只会作为文字发送`);
    }
    this.tui.requestRender();
    const chatId = await this.ensureSession(text);
    if (!chatId) return;
    const message = this.bashContext.length ? [...this.bashContext, text].join('\n\n') : text;
    this.bashContext = [];
    const abort = new AbortController();
    const run = this.beginRun(abort);
    let stream: AsyncIterable<ChatEvent>;
    try {
      stream = this.engine.send(chatId, message, { model: this.model, signal: abort.signal });
    } catch (error) {
      this.transcript.error(errorMessage(error));
      this.endRun(run);
      return;
    }
    await this.follow(run, stream);
  }

  private beginRun(abort: AbortController, runId?: string): RunState {
    const run: RunState = { abort, startedAt: Date.now(), cancelRequested: false, queued: [], runId };
    this.run = run;
    this.transcript.beginRun();
    this.loader = new Loader(this.tui, theme.accent, theme.muted, '工作中… (esc 中断)');
    this.renderStatus();
    this.footer.update({ runStartedAt: run.startedAt, runState: undefined });
    this.tui.requestRender();
    return run;
  }

  private async follow(run: RunState, stream: AsyncIterable<ChatEvent>): Promise<void> {
    let failed = false;
    try {
      for await (const event of stream) {
        if (this.run !== run) break;
        for (const signal of this.transcript.apply(event)) await this.onSignal(run, signal);
        this.tui.requestRender();
      }
    } catch (error) {
      if (!run.abort.signal.aborted) {
        failed = true;
        this.transcript.current?.addNotice('✗ ' + errorMessage(error), theme.error);
      }
    }
    await this.finishRun(run, failed);
  }

  private async onSignal(run: RunState, signal: Signal): Promise<void> {
    switch (signal.type) {
      case 'run_started':
        run.runId = signal.runId;
        if (run.cancelRequested) void this.engine.cancel(signal.runId).catch(() => {});
        for (const item of run.queued) void this.deliver(run, item);
        break;
      case 'confirm':
        this.enqueuePrompt({ kind: 'confirm', id: signal.request.confirmId, request: signal.request });
        break;
      case 'confirm_expired':
        if (this.removePrompt(signal.confirmId)) this.transcript.current?.addNotice('· 一项确认已超时取消');
        break;
      case 'question':
        this.enqueuePrompt({ kind: 'question', id: signal.request.requestId, request: signal.request });
        break;
      case 'question_resolved':
        this.removePrompt(signal.requestId);
        break;
      case 'context':
        this.footer.update({ contextPercent: signal.percent });
        break;
      case 'plan':
        run.plan = signal.plan;
        this.planBox.clear();
        this.planBox.addChild(new PlanView(signal.plan));
        break;
      case 'queued_run':
        run.handoff = { runId: signal.handoff.runId, message: signal.handoff.message };
        this.dropQueued(run, signal.handoff.message);
        break;
      case 'steer_applied':
        this.dropQueued(run, signal.message);
        this.transcript.beginTurn();
        break;
      case 'error': case 'end':
        break;
    }
  }

  /** The backend took a queued input: stop showing it as pending. */
  private dropQueued(run: RunState, message: string): void {
    const index = run.queued.findIndex((q) => q.text.trim() === message.trim());
    run.queued.splice(index >= 0 ? index : 0, run.queued.length ? 1 : 0);
    this.renderPending();
  }

  private endRun(run: RunState): void {
    if (this.run !== run) return;
    this.run = undefined;
    this.loader?.stop();
    this.loader = undefined;
    this.renderStatus();
    this.footer.update({ runStartedAt: undefined, runState: undefined });
    this.clearPrompts();
    this.tui.requestRender();
  }

  private async finishRun(run: RunState, failed: boolean): Promise<void> {
    if (this.run !== run) return;
    const interrupted = run.cancelRequested;
    this.transcript.endRun(interrupted);
    if (interrupted) this.transcript.notice('◼ 已中断', theme.warning);
    if (run.plan) {
      this.transcript.add(new PlanView(run.plan));
      this.planBox.clear();
    }
    const leftover = run.queued;
    this.endRun(run);
    if (failed || interrupted || this.shuttingDown) {
      this.renderPending();
      return;
    }
    // A queued message the backend turned into its own run: follow it.
    if (run.handoff) {
      this.transcript.addUser(run.handoff.message, '排队');
      return this.attach({ runId: run.handoff.runId });
    }
    if (leftover.length && this.chatId) {
      const active = await this.engine.activeRun(this.chatId).catch(() => null);
      if (active) return this.attach(active);
      this.nextMessages.push(...leftover.map((q) => q.text));
    }
    this.renderPending();
    if (this.nextMessages.length) {
      const text = this.nextMessages.join('\n\n');
      this.nextMessages = [];
      this.renderPending();
      await this.sendMessage(text);
    }
  }

  /** Follow a run that is already going (resume, handoff). */
  private async attach(active: ActiveRun): Promise<void> {
    const abort = new AbortController();
    const run = this.beginRun(abort, active.runId);
    let stream: AsyncIterable<ChatEvent>;
    try {
      stream = this.engine.resume(active.runId, active.offset, abort.signal);
    } catch (error) {
      this.transcript.error(errorMessage(error));
      this.endRun(run);
      return;
    }
    await this.follow(run, stream);
  }

  async cancelRun(): Promise<void> {
    const run = this.run;
    if (!run || run.cancelRequested) return;
    run.cancelRequested = true;
    this.transcript.cancelling = true;
    // Like pi: queued inputs the run never applied go back into the editor.
    const unsent = [...run.queued.map((q) => q.text), ...this.nextMessages];
    run.queued = [];
    this.nextMessages = [];
    if (unsent.length) {
      this.editor.setText([...unsent, this.editor.getText()].filter((t) => t.trim()).join('\n\n'));
    }
    this.renderPending();
    this.clearPrompts();
    this.footer.update({ runState: '正在中断…' });
    this.loader?.setMessage('正在中断…');
    this.tui.requestRender();
    if (run.runId) {
      try {
        await this.engine.cancel(run.runId);
      } catch (error) {
        this.transcript.current?.addNotice('✗ 中断失败：' + errorMessage(error), theme.error);
      }
    }
    setTimeout(() => { if (this.run === run) run.abort.abort(); }, run.runId ? CANCEL_GRACE_MS : 0).unref?.();
  }

  // ── Inline prompts ───────────────────────────────────────────────────────

  private enqueuePrompt(entry: PendingPrompt): void {
    if (this.prompts.some((p) => p.id === entry.id)) return;
    this.prompts.push(entry);
    if (!this.activePrompt) this.showNextPrompt();
  }

  private removePrompt(id: string): boolean {
    const before = this.prompts.length;
    this.prompts = this.prompts.filter((p) => p.id !== id);
    if (this.activePrompt?.entry.id === id) {
      this.activePrompt = undefined;
      this.showNextPrompt();
    }
    return this.prompts.length !== before;
  }

  private clearPrompts(): void {
    this.prompts = [];
    if (this.activePrompt) {
      this.activePrompt = undefined;
      this.showNextPrompt();
    }
  }

  private showNextPrompt(): void {
    const entry = this.prompts[0];
    if (!entry) {
      this.restoreEditor();
      return;
    }
    const component = entry.kind === 'confirm'
      ? createConfirmPrompt(entry.request, (d) => void this.answerConfirm(entry, d))
      : new QuestionPrompt(entry.request, (answers) => void this.answerQuestion(entry, answers));
    this.activePrompt = { entry, component };
    this.editorContainer.clear();
    this.editorContainer.addChild(component);
    this.tui.setFocus(component);
    this.loader?.setMessage(entry.kind === 'confirm' ? '等待你确认…' : '等待你回答…');
    this.footer.update({ runState: entry.kind === 'confirm' ? '等待确认' : '等待回答' });
    this.tui.requestRender();
  }

  private restoreEditor(): void {
    if (this.editorContainer.children[0] !== this.editor) {
      this.editorContainer.clear();
      this.editorContainer.addChild(this.editor);
    }
    this.tui.setFocus(this.editor);
    if (this.run && !this.run.cancelRequested) {
      this.loader?.setMessage('工作中… (esc 中断)');
      this.footer.update({ runState: undefined });
    }
    this.tui.requestRender();
  }

  private async answerConfirm(entry: Extract<PendingPrompt, { kind: 'confirm' }>, d: ConfirmDecision): Promise<void> {
    this.removePrompt(entry.id);
    const chosen = d.optionId ? entry.request.options.find((o) => o.id === d.optionId)?.title : undefined;
    const label = { allow: '已允许', allow_session: '本会话内已允许', deny: '已拒绝', choice: `已选择「${chosen ?? d.optionId}」`, skip: '交给助手决定' }[d.decision];
    const style = d.decision === 'deny' ? theme.warning : theme.dim;
    this.transcript.current?.addNotice(`${d.decision === 'deny' ? '✗' : '✓'} ${label} · ${confirmTitle(entry.request).replace(/^需要确认 · /, '')}`, style);
    this.tui.requestRender();
    if (!this.chatId) return;
    try {
      await this.engine.confirm(this.chatId, entry.id, d.decision, d.optionId);
    } catch (error) {
      this.transcript.current?.addNotice('✗ 确认没送达：' + errorMessage(error), theme.error);
      this.tui.requestRender();
    }
  }

  private async answerQuestion(entry: Extract<PendingPrompt, { kind: 'question' }>, answers: QuestionAnswer[]): Promise<void> {
    this.removePrompt(entry.id);
    const summary = answers.map((a) => {
      const q = entry.request.questions.find((x) => x.id === a.id);
      if (a.skipped) return '跳过';
      const picked = a.selected.map((id) => q?.options.find((o) => o.id === id)?.label ?? id);
      return [...picked, ...(a.custom ? [a.custom] : [])].join('、');
    }).join(' | ');
    this.transcript.current?.addNotice(`? 已回答：${oneLine(summary, 120)}`);
    this.tui.requestRender();
    if (!this.chatId) return;
    try {
      await this.engine.answer(this.chatId, entry.id, answers);
    } catch (error) {
      this.transcript.current?.addNotice('✗ 回答没送达：' + errorMessage(error), theme.error);
      this.tui.requestRender();
    }
  }

  /** Pick-one selector in the editor's place (slash commands). */
  private select(title: string, items: { value: string; label: string; description?: string }[], initialValue?: string, body?: string): Promise<string | undefined> {
    return new Promise((resolve) => {
      const finish = (value?: string) => {
        this.activePrompt = undefined;
        if (this.prompts.length) this.showNextPrompt();
        else this.restoreEditor();
        resolve(value);
      };
      const component = new TitledSelect({ title, body, items, initialValue, onSelect: (item) => finish(item.value), onCancel: () => finish() });
      this.editorContainer.clear();
      this.editorContainer.addChild(component);
      this.tui.setFocus(component);
      this.tui.requestRender();
    });
  }

  // ── Slash commands ───────────────────────────────────────────────────────

  private async runCommand(name: string, args: string): Promise<void> {
    try {
      switch (name) {
        case 'mode': return await this.cmdMode(args);
        case 'model': return await this.cmdModel();
        case 'new': return this.cmdNew();
        case 'resume': return await this.cmdResume();
        case 'fork': return await this.cmdFork();
        case 'compact': return await this.cmdCompact();
        case 'permissions': return await this.cmdPermissions(args);
        case 'copy': return this.cmdCopy();
        case 'hotkeys': return this.info('快捷键', HOTKEYS_TEXT);
        case 'help': return this.info('命令', HELP_TEXT);
        case 'quit': return await this.shutdown();
      }
    } catch (error) {
      this.transcript.error(`/${name} 失败：${errorMessage(error)}`);
      this.restoreEditor();
    } finally {
      // Every case returns, so redraw here: a slow command (/compact) ends long after any other repaint.
      if (name !== 'quit') this.tui.requestRender();
    }
  }

  private info(title: string, body: string): void {
    this.transcript.notice(theme.bold(title) + '\n' + body, theme.muted);
    this.tui.requestRender();
  }

  private busy(): boolean {
    if (!this.run) return false;
    this.flashHint('运行中：先按 esc 中断，或等它结束');
    return true;
  }

  private async cmdMode(arg: string): Promise<void> {
    let target = arg;
    if (!target) {
      const current = this.engine.info.mode;
      const items = [
        { value: 'local', label: 'local', description: '在本机当前目录工作' },
        { value: 'cloud', label: 'cloud', description: '远端服务，访问不到本机文件' },
      ];
      if (current.startsWith('device:')) items.push({ value: current, label: current, description: '远程设备' });
      const picked = await this.select(`执行位置（当前 ${current}）`, items, current, '远程设备用 /mode device:<id>');
      if (!picked) return;
      target = picked;
    }
    if (target !== 'local' && target !== 'cloud' && !/^device:.+/.test(target)) {
      this.transcript.error(`未知的执行位置：${target}（可选 local / cloud / device:<id>）`);
      return;
    }
    if (target === this.engine.info.mode) return this.flashHint(`已经在 ${target}`);
    if (this.busy()) return;
    this.switching = true;
    this.flashHint(`正在连接 ${target}…`);
    this.tui.requestRender();
    try {
      const next = await this.options.createEngine(target as Mode, { cwd: this.options.cwd });
      await next.connect();
      const previous = this.engine;
      this.engine = next;
      void previous.close().catch(() => {});
      this.chatId = undefined;
      this.footer.update({ info: next.info, contextPercent: undefined, sessionTitle: undefined, model: this.modelLabel ?? this.model?.modelName, permission: undefined });
      this.transcript.divider(`${next.info.label}${next.info.canAccessCwd ? '' : ' · 无本地文件访问'}`);
      try {
        const session = await next.newSession();
        this.chatId = session.chatId;
      } catch (error) {
        this.transcript.notice(`· 新会话会在第一条消息时创建（${errorMessage(error)}）`);
      }
      void this.refreshEngineState();
    } catch (error) {
      this.transcript.error(`切换到 ${target} 失败：${errorMessage(error)}`);
    } finally {
      this.switching = false;
      this.tui.requestRender();
    }
  }

  private async cmdModel(): Promise<void> {
    const models = await this.engine.listModels();
    if (!models.length) return this.transcript.notice('· 没有可用的模型');
    const key = (m: { providerId: string; modelName: string }) => `${m.providerId}\u0000${m.modelName}`;
    const current = this.model ? key(this.model) : models.find((m) => m.current) ? key(models.find((m) => m.current)!) : undefined;
    const picked = await this.select('选择模型（对之后的消息生效）', models.map((m) => ({
      value: key(m), label: m.label || m.modelName, description: m.providerId + (m.current ? ' · 默认' : ''),
    })), current);
    if (!picked) return;
    const model = models.find((m) => key(m) === picked)!;
    this.model = { providerId: model.providerId, modelName: model.modelName };
    this.modelLabel = model.label || model.modelName;
    this.footer.update({ model: model.label || model.modelName });
    this.flashHint(`模型：${model.label || model.modelName}`);
  }

  private cmdNew(): void {
    if (this.busy()) return;
    this.chatId = undefined;
    this.footer.update({ contextPercent: undefined, sessionTitle: undefined });
    this.transcript.divider('新会话');
  }

  private async cmdResume(): Promise<void> {
    if (this.busy()) return;
    const sessions = await this.engine.listSessions(30);
    if (!sessions.length) return this.transcript.notice('· 这里还没有会话');
    const picked = await this.select('恢复会话', sessions.map((s) => ({
      value: s.chatId, label: s.title || '(无标题)', description: s.updatedAt ? relativeTime(s.updatedAt) : undefined,
    })), this.chatId);
    if (picked) await this.loadSession(picked, sessions.find((s) => s.chatId === picked)?.title);
  }

  async loadSession(chatId: string, title?: string): Promise<void> {
    this.chatId = chatId;
    this.transcript.divider(`会话 · ${title || chatId}`);
    this.footer.update({ sessionTitle: title, contextPercent: undefined });
    this.tui.requestRender();
    try {
      const [history, active] = await Promise.all([
        this.engine.history(chatId, 50),
        this.engine.activeRun(chatId).catch(() => null),
      ]);
      // The in-flight reply comes back through the run's snapshot instead.
      this.transcript.renderHistory(active ? history.filter((m) => !m.in_flight) : history);
      this.tui.requestRender();
      if (active) await this.attach(active);
    } catch (error) {
      this.transcript.error(`读取会话失败：${errorMessage(error)}`);
      this.tui.requestRender();
    }
  }

  private async cmdFork(): Promise<void> {
    if (!this.chatId) return this.transcript.notice('· 还没有会话可以分叉');
    if (this.busy()) return;
    const session = await this.engine.fork(this.chatId);
    this.chatId = session.chatId;
    this.footer.update({ sessionTitle: session.title });
    this.transcript.divider(`已分叉 · ${session.title || session.chatId}`);
  }

  private async cmdCompact(): Promise<void> {
    if (!this.chatId) return this.transcript.notice('· 还没有会话需要压缩');
    if (this.busy()) return;
    this.flashHint('正在压缩上下文…');
    this.tui.requestRender();
    const result = await this.engine.compact(this.chatId);
    const tokens = (count: number | null) => (count === null ? '?' : formatTokens(count));
    if (result.compacted) {
      this.transcript.notice(`✓ 已压缩上下文 · ${tokens(result.tokensBefore)} → 约 ${tokens(result.tokensAfter)} tokens`);
    } else {
      // The server keeps the recent turns verbatim; a short session has nothing older to fold.
      this.transcript.notice(
        `· 无需压缩${result.tokensBefore === null ? '' : ` · 当前约 ${tokens(result.tokensBefore)} tokens`}，最近的对话会原样保留`,
      );
    }
  }

  private async cmdPermissions(arg: string): Promise<void> {
    const modes: PermissionMode[] = ['ask', 'auto', 'full'];
    let mode = arg as PermissionMode;
    if (!arg) {
      const current = await this.engine.getPermissionMode().catch(() => undefined);
      const picked = await this.select('工具审批档位', [
        { value: 'ask', label: 'ask', description: '写文件、执行命令前先问我' },
        { value: 'auto', label: 'auto', description: '常规操作自动放行，危险操作才问' },
        { value: 'full', label: 'full', description: '全部放行' },
      ], current);
      if (!picked) return;
      mode = picked as PermissionMode;
    }
    if (!modes.includes(mode)) return this.transcript.error(`未知的审批档位：${arg}（可选 ask / auto / full）`);
    await this.engine.setPermissionMode(mode);
    this.footer.update({ permission: mode });
    this.flashHint(PERMISSION_LABELS[mode]);
  }

  private cmdCopy(): void {
    const text = this.transcript.lastAnswer;
    if (!text) return this.flashHint('还没有回答可以复制');
    copyToClipboard(text, (data) => this.tui.terminal.write(data));
    this.flashHint(`已复制 ${text.length} 字`);
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms).unref?.());
}


function relativeTime(iso: string): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const s = Math.round((Date.now() - t) / 1000);
  if (s < 60) return '刚刚';
  if (s < 3600) return `${Math.round(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.round(s / 3600)} 小时前`;
  return `${Math.round(s / 86400)} 天前`;
}

