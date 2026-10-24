/**
 * Engine events → transcript components (text / thinking / tool lines / notices).
 *
 * `Transcript.apply()` folds one SSE event into the current assistant turn and returns the
 * signals the app must act on (prompts, footer, run bookkeeping). Rendering is pure
 * `render(width)`, so it can be tested without a terminal.
 */
import { Box, Container, Markdown, Spacer, truncateToWidth, visibleWidth, wrapTextWithAnsi, type Component } from '@mariozechner/pi-tui';
import type { ChatEvent, ChatHistoryMessage } from '../engine/engine.js';
import {
  errorText, parseConfirm, parsePlan, parseQuestion, parseQueuedRun,
  type ConfirmRequest, type Plan, type QuestionRequest, type QueuedRunHandoff,
} from './events.js';
import { contextPercent, formatDuration, oneLine, outputText, parseArgs, resultSize, str, summarizeArgs } from './format.js';
import { markdownTheme, theme } from './theme.js';

/** Display toggles shared by every component (Ctrl+T thinking, Ctrl+O tools). */
export interface ViewState { expandThinking: boolean; expandTools: boolean }

export type Signal =
  | { type: 'run_started'; runId: string }
  | { type: 'confirm'; request: ConfirmRequest }
  | { type: 'confirm_expired'; confirmId: string }
  | { type: 'question'; request: QuestionRequest }
  | { type: 'question_resolved'; requestId: string; outcome: string }
  | { type: 'context'; percent: number }
  | { type: 'plan'; plan: Plan }
  | { type: 'queued_run'; handoff: QueuedRunHandoff }
  | { type: 'steer_applied'; message: string }
  | { type: 'error'; message: string }
  | { type: 'end' };

const SPINNER = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];
const spinner = () => SPINNER[Math.floor(Date.now() / 80) % SPINNER.length];

/** Fixed-width lines: every line a component returns must fit the terminal. */
function fit(line: string, width: number): string {
  return visibleWidth(line) > width ? truncateToWidth(line, width) : line;
}

function indentWrap(text: string, width: number, indent: string, style: (s: string) => string, maxLines: number): string[] {
  const inner = Math.max(10, width - visibleWidth(indent));
  const raws = text.split('\n');
  const out: string[] = [];
  // Wrap only what is shown; the rest is counted in source lines, not wrapped ones.
  for (let i = 0; i < raws.length; i++) {
    const wrapped = wrapTextWithAnsi(raws[i], inner);
    for (const line of wrapped) out.push(fit(indent + style(line), width));
    if (out.length > maxLines) {
      const hidden = out.length - maxLines + raws.length - i - 1;
      return [...out.slice(0, maxLines), fit(indent + theme.dim(`… 还有 ${hidden} 行`), width)];
    }
  }
  return out;
}

// ── Leaf components ──────────────────────────────────────────────────────────

export class UserMessage implements Component {
  private box: Box;
  constructor(public readonly text: string, label?: string) {
    this.box = new Box(1, 1, (s) => theme.userBg(s));
    const body = label ? `${theme.dim(label)} ${text}` : text;
    this.box.addChild(new Markdown(body, 0, 0, markdownTheme));
  }
  render(width: number): string[] { return this.box.render(width); }
  invalidate(): void { this.box.invalidate(); }
}

/** A dim / coloured one-off line (errors, notices, dividers). */
export class Notice implements Component {
  constructor(private text: string, private style: (s: string) => string = theme.dim) {}
  setText(text: string, style?: (s: string) => string): void {
    this.text = text;
    if (style) this.style = style;
  }
  render(width: number): string[] {
    return this.text.split('\n').flatMap((l) => wrapTextWithAnsi(this.style(l), Math.max(1, width - 1)).map((w) => fit(' ' + w, width)));
  }
  invalidate(): void {}
}

export class Divider implements Component {
  constructor(private label = '') {}
  render(width: number): string[] {
    if (!this.label) return [theme.borderMuted('─'.repeat(Math.max(1, width)))];
    const head = `── ${this.label} `;
    const rest = Math.max(0, width - visibleWidth(head));
    return [fit(theme.borderMuted('── ') + theme.muted(this.label + ' ') + theme.borderMuted('─'.repeat(rest)), width)];
  }
  invalidate(): void {}
}

export class TextBlock implements Component {
  private md: Markdown;
  text = '';
  constructor(text = '') {
    this.md = new Markdown('', 1, 0, markdownTheme);
    this.setText(text);
  }
  setText(text: string): void {
    this.text = text;
    this.md.setText(text.trim());
  }
  append(delta: string): void { this.setText(this.text + delta); }
  render(width: number): string[] { return this.text.trim() ? this.md.render(width) : []; }
  invalidate(): void { this.md.invalidate(); }
}

export class ThinkingBlock implements Component {
  text = '';
  readonly startedAt: number;
  endedAt?: number;
  private md = new Markdown('', 1, 0, markdownTheme, { color: theme.thinking, italic: true });
  constructor(private view: ViewState, startedAt = Date.now(), private fixedSeconds?: number) {
    this.startedAt = startedAt;
  }
  append(delta: string): void {
    this.text += delta;
    this.md.setText(this.text.trim());
  }
  end(at = Date.now()): void { if (this.endedAt === undefined) this.endedAt = at; }
  get seconds(): number {
    if (this.fixedSeconds !== undefined) return this.fixedSeconds;
    return Math.max(1, Math.round(((this.endedAt ?? Date.now()) - this.startedAt) / 1000));
  }
  render(width: number): string[] {
    const active = this.endedAt === undefined;
    const label = active ? `${spinner()} 思考中 ${this.seconds} 秒…` : this.fixedSeconds === 0 ? '思考' : `思考 ${this.seconds} 秒`;
    const hint = this.text.trim() && !this.view.expandThinking ? theme.dim('  ctrl+t 展开') : '';
    const head = fit(' ' + theme.thinking(theme.italic(label)) + hint, width);
    if (!this.view.expandThinking || !this.text.trim()) return [head];
    return [head, ...this.md.render(width)];
  }
  invalidate(): void { this.md.invalidate(); }
}

export type ToolStatus = 'running' | 'success' | 'error' | 'interrupted';

interface SubStep { toolId?: string; name: string; input?: unknown; inputText?: string; output?: unknown; status: ToolStatus; text?: string }

export class ToolLine implements Component {
  name: string;
  displayName?: string;
  input?: unknown;
  argsText = '';
  output?: unknown;
  error?: string;
  status: ToolStatus = 'running';
  progress?: string;
  durationMs?: number;
  readonly startedAt = Date.now();
  subSteps: SubStep[] = [];
  /** Summary and result size, recomputed only when the input or output they came from changes. */
  private derived: { args?: unknown; argsText?: string; summary?: string; output?: unknown; size?: string } = {};
  constructor(private view: ViewState, public id: string, name: string) {
    this.name = name || 'tool';
  }

  private summary(): string {
    const d = this.derived;
    if (d.summary === undefined || d.args !== this.input || d.argsText !== this.argsText) {
      Object.assign(d, { args: this.input, argsText: this.argsText, summary: summarizeArgs(this.args()) });
    }
    return d.summary!;
  }

  private size(): string {
    const d = this.derived;
    if (d.size === undefined || d.output !== this.output) Object.assign(d, { output: this.output, size: resultSize(this.output) });
    return d.size!;
  }

  finish(status: ToolStatus): void {
    this.status = status;
    if (this.durationMs === undefined) this.durationMs = Date.now() - this.startedAt;
  }

  private args(): unknown { return this.input ?? (this.argsText ? parseArgs(this.argsText) : undefined); }

  private headline(width: number): string {
    const icon = this.status === 'running' ? theme.accent(spinner())
      : this.status === 'success' ? theme.success('✓')
        : this.status === 'interrupted' ? theme.warning('◼') : theme.error('✗');
    const parts = [this.displayName || this.name];
    const summary = this.summary();
    if (summary) parts.push(summary);
    if (this.status === 'running') {
      if (this.progress) parts.push(this.progress);
      parts.push(formatDuration(Date.now() - this.startedAt));
    } else {
      if (this.status === 'error') parts.push(oneLine(this.error || errorText(this.output) || '失败', 80));
      else if (this.status === 'interrupted') parts.push('已中断');
      else {
        const size = this.size();
        if (size) parts.push(size);
      }
      if (this.durationMs !== undefined) parts.push(formatDuration(this.durationMs));
    }
    const style = this.status === 'error' ? theme.error : theme.muted;
    const [name, ...rest] = parts;
    const text = (this.status === 'error' ? theme.error(name) : name) + (rest.length ? style(' · ' + rest.join(' · ')) : '');
    return fit(` ${icon} ${text}`, width);
  }

  private subLine(step: SubStep, width: number): string {
    if (step.text !== undefined) return fit('   ' + theme.dim('└ ' + oneLine(step.text, 100)), width);
    const icon = step.status === 'running' ? theme.accent(spinner()) : step.status === 'success' ? theme.success('✓') : theme.error('✗');
    const summary = summarizeArgs(step.input ?? (step.inputText ? parseArgs(step.inputText) : undefined));
    const size = step.status === 'success' ? resultSize(step.output) : '';
    const rest = [summary, size].filter(Boolean).join(' · ');
    return fit(`   ${theme.dim('└')} ${icon} ${theme.muted(step.name + (rest ? ' · ' + rest : ''))}`, width);
  }

  render(width: number): string[] {
    const lines = [this.headline(width)];
    const tools = this.subSteps.filter((s) => s.text === undefined || this.view.expandTools);
    const shown = this.view.expandTools ? tools : tools.slice(-5);
    if (shown.length < tools.length) lines.push(fit('   ' + theme.dim(`└ … 前面还有 ${tools.length - shown.length} 步`), width));
    for (const step of shown) lines.push(this.subLine(step, width));
    if (this.view.expandTools) {
      const args = this.args();
      if (args !== undefined) lines.push(...indentWrap(outputText(args), width, '     ', theme.dim, 8));
      const out = this.status === 'error' && this.error ? this.error : outputText(this.output);
      if (out) lines.push(...indentWrap(out, width, '   │ ', this.status === 'error' ? theme.error : theme.muted, 14));
    }
    return lines;
  }

  invalidate(): void {}

  applySubagent(ev: Record<string, unknown>): void {
    const subType = str(ev.sub_type ?? ev.subType);
    if (subType === 'job_progress') {
      const total = Number(ev.total) || 0;
      this.progress = total ? `${Number(ev.settled) || 0}/${total}` : '准备中';
      return;
    }
    const agent = str(ev.agent_name);
    if (agent) this.displayName = `${this.name} · ${agent}`;
    const tid = str(ev.tool_id);
    const find = () => (tid ? this.subSteps.find((s) => s.toolId === tid) : undefined);
    const upsert = (patch: Partial<SubStep>) => {
      const step = find();
      if (step) Object.assign(step, patch);
      else this.subSteps.push({ toolId: tid || undefined, name: str(ev.tool_name) || 'tool', status: 'running', ...patch });
    };
    if (subType === 'tool_call') upsert(ev.input != null ? { input: ev.input } : {});
    else if (subType === 'tool_call_delta') upsert({ inputText: (find()?.inputText ?? '') + str(ev.arguments_delta) });
    else if (subType === 'tool_result') upsert({ status: str(ev.status) === 'error' ? 'error' : 'success', output: ev.output });
    else if (subType === 'content' || subType === 'thinking') {
      const last = this.subSteps[this.subSteps.length - 1];
      if (last && last.text !== undefined) last.text += str(ev.delta);
      else this.subSteps.push({ name: '', status: 'success', text: str(ev.delta) });
    } else if (subType === 'error') this.subSteps.push({ name: '', status: 'error', text: '⚠ ' + (errorText(ev.error) || 'error') });
    else if (subType === 'end') this.finish(ev.ok === true ? 'success' : str(ev.status) === 'cancelled' ? 'interrupted' : 'error');
  }
}

// ── Inline <think> splitting ────────────────────────────────────────────────

/** Splits `<think>…</think>` out of streamed content; holds back a possibly split tag. */
export class ThinkSplitter {
  private inThink = false;
  private carry = '';
  feed(delta: string): { kind: 'text' | 'thinking'; text: string }[] {
    let buf = this.carry + delta;
    this.carry = '';
    const out: { kind: 'text' | 'thinking'; text: string }[] = [];
    while (buf) {
      const tag = this.inThink ? '</think>' : '<think>';
      const at = buf.indexOf(tag);
      if (at >= 0) {
        if (at > 0) out.push({ kind: this.inThink ? 'thinking' : 'text', text: buf.slice(0, at) });
        buf = buf.slice(at + tag.length);
        this.inThink = !this.inThink;
        continue;
      }
      // Hold back a trailing prefix of the tag ("<thi") until the next delta decides it.
      let hold = 0;
      for (let n = Math.min(tag.length - 1, buf.length); n > 0; n--) {
        if (tag.startsWith(buf.slice(-n))) { hold = n; break; }
      }
      const emit = buf.slice(0, buf.length - hold);
      if (emit) out.push({ kind: this.inThink ? 'thinking' : 'text', text: emit });
      this.carry = buf.slice(buf.length - hold);
      buf = '';
    }
    return out;
  }
  flush(): { kind: 'text' | 'thinking'; text: string }[] {
    const rest = this.carry;
    this.carry = '';
    return rest ? [{ kind: this.inThink ? 'thinking' : 'text', text: rest }] : [];
  }
}

// ── Assistant turn ──────────────────────────────────────────────────────────

type Segment = TextBlock | ThinkingBlock | ToolLine | Notice;

/** One assistant reply: segments in arrival order, blank lines only around prose. */
export class AssistantTurn implements Component {
  segments: Segment[] = [];
  private compactions = new Map<string, Notice>();
  private splitter = new ThinkSplitter();
  /** `tools` is shared by every turn of one run: a call may finish after a steer split the reply. */
  constructor(private view: ViewState, private tools = new Map<string, ToolLine>()) {}

  get text(): string {
    return this.segments.filter((s): s is TextBlock => s instanceof TextBlock).map((s) => s.text).join('\n\n').trim();
  }
  get isEmpty(): boolean { return this.segments.length === 0; }

  private last(): Segment | undefined { return this.segments[this.segments.length - 1]; }

  private endThinking(): void {
    const last = this.last();
    if (last instanceof ThinkingBlock) last.end();
  }

  appendText(delta: string): void {
    for (const part of this.splitter.feed(delta)) {
      if (part.kind === 'thinking') this.appendThinking(part.text);
      else this.appendPlain(part.text);
    }
  }

  private appendPlain(delta: string): void {
    if (!delta) return;
    const last = this.last();
    if (last instanceof TextBlock) return last.append(delta);
    if (!delta.trim()) return;
    this.endThinking();
    this.segments.push(new TextBlock(delta));
  }

  replaceText(text: string): void {
    this.segments = this.segments.filter((s) => !(s instanceof TextBlock));
    this.endThinking();
    this.segments.push(new TextBlock(text));
  }

  appendThinking(delta: string): void {
    if (!delta) return;
    const last = this.last();
    if (last instanceof ThinkingBlock && last.endedAt === undefined) return last.append(delta);
    const block = new ThinkingBlock(this.view);
    block.append(delta);
    this.segments.push(block);
  }

  addNotice(text: string, style?: (s: string) => string): Notice {
    this.endThinking();
    const notice = new Notice(text, style);
    this.segments.push(notice);
    return notice;
  }

  tool(id: string, name: string): ToolLine {
    let line = id ? this.tools.get(id) : undefined;
    if (!line) {
      this.endThinking();
      line = new ToolLine(this.view, id, name);
      if (id) this.tools.set(id, line);
      this.segments.push(line);
    } else if (name && line.name === 'tool') line.name = name;
    return line;
  }

  /** tool_result without an id matches the newest running call with that name. */
  findTool(id: string, name: string): ToolLine | undefined {
    if (id && this.tools.has(id)) return this.tools.get(id);
    const running = [...this.tools.values()].reverse();
    return running.find((t) => t.status === 'running' && (!name || t.name === name)) ?? undefined;
  }

  allTools(): ToolLine[] { return [...this.tools.values()]; }

  compaction(ev: Record<string, unknown>): void {
    const id = str(ev.id) || 'compaction';
    const status = str(ev.status);
    const label = status === 'running' ? '⟳ 正在压缩上下文…'
      : status === 'completed' ? '✓ 已压缩上下文'
        : status === 'skipped' ? '· 跳过上下文压缩'
          : `✗ 上下文压缩${status === 'interrupted' ? '中断' : '失败'}${ev.reason ? '：' + str(ev.reason) : ''}`;
    const style = status === 'failed' || status === 'interrupted' ? theme.error : theme.dim;
    const existing = this.compactions.get(id);
    if (existing) existing.setText(label, style);
    else this.compactions.set(id, this.addNotice(label, style));
  }

  /**
   * Close the turn. `boundary` (a steer split the reply) leaves tool calls running; `end` and
   * `interrupted` settle them too.
   */
  finish(mode: 'boundary' | 'end' | 'interrupted' = 'end'): void {
    for (const part of this.splitter.flush()) {
      if (part.kind === 'thinking') this.appendThinking(part.text);
      else this.appendPlain(part.text);
    }
    for (const seg of this.segments) {
      if (seg instanceof ThinkingBlock) seg.end();
      if (mode !== 'boundary' && seg instanceof ToolLine && seg.status === 'running') seg.finish(mode === 'interrupted' ? 'interrupted' : 'success');
    }
  }

  render(width: number): string[] {
    const lines: string[] = [];
    let prev: Segment | undefined;
    for (const seg of this.segments) {
      const out = seg.render(width);
      if (!out.length) continue;
      const prose = seg instanceof TextBlock;
      const open = this.view.expandThinking && (seg instanceof ThinkingBlock || prev instanceof ThinkingBlock);
      if (prev && (prose || prev instanceof TextBlock || open)) lines.push('');
      lines.push(...out);
      prev = seg;
    }
    return lines.length ? ['', ...lines] : [];
  }

  invalidate(): void { for (const s of this.segments) s.invalidate(); }
}

// ── Transcript ──────────────────────────────────────────────────────────────

export class Transcript {
  readonly container = new Container();
  current?: AssistantTurn;
  private lastAnswerText = '';
  private runTools = new Map<string, ToolLine>();
  /** Set once the user cancelled: the backend's terminal error is expected, not news. */
  cancelling = false;
  constructor(readonly view: ViewState) {}

  add(component: Component): void { this.container.addChild(component); }

  addUser(text: string, label?: string): void {
    this.add(new Spacer(1));
    this.add(new UserMessage(text, label));
  }

  notice(text: string, style: (s: string) => string = theme.dim): void {
    this.add(new Spacer(1));
    this.add(new Notice(text, style));
  }

  error(text: string): void { this.notice('✗ ' + text, theme.error); }

  divider(label: string): void {
    this.add(new Spacer(1));
    this.add(new Divider(label));
  }

  /** A new run: fresh tool index, first turn. */
  beginRun(): AssistantTurn {
    this.endTurn();
    this.runTools = new Map();
    this.cancelling = false;
    return this.beginTurn();
  }

  /** Settle the run: close the open turn and any call left running in an earlier turn. */
  endRun(interrupted: boolean): void {
    this.endTurn(interrupted ? 'interrupted' : 'end');
    for (const tool of this.runTools.values()) {
      if (tool.status === 'running') tool.finish(interrupted ? 'interrupted' : 'success');
    }
  }

  beginTurn(): AssistantTurn {
    this.endTurn('boundary');
    this.current = new AssistantTurn(this.view, this.runTools);
    this.add(this.current);
    return this.current;
  }

  endTurn(mode: 'boundary' | 'end' | 'interrupted' = 'end'): void {
    if (!this.current) return;
    this.current.finish(mode);
    if (this.current.text) this.lastAnswerText = this.current.text;
    if (this.current.isEmpty) this.container.removeChild(this.current);
    this.current = undefined;
  }

  get lastAnswer(): string { return this.current?.text || this.lastAnswerText; }

  private turn(): AssistantTurn { return this.current ?? this.beginTurn(); }

  /** Fold one event into the transcript; returns what the app has to do about it. */
  apply(event: ChatEvent): Signal[] {
    const ev = event as Record<string, unknown>;
    const type = str(ev.type);
    const signals: Signal[] = [];
    const usage = (raw: unknown) => {
      const percent = contextPercent(raw);
      if (percent !== undefined) signals.push({ type: 'context', percent });
    };
    switch (type) {
      case 'run_started':
        if (ev.run_id) signals.push({ type: 'run_started', runId: str(ev.run_id) });
        break;
      case 'content': case 'ai_message': case 'text': case 'delta': {
        const delta = str(ev.delta ?? ev.content ?? ev.text);
        if (delta) this.turn().appendText(delta);
        break;
      }
      case 'content_replace':
        this.turn().replaceText(str(ev.content ?? ev.text));
        break;
      case 'thinking': case 'thought': {
        const delta = str(ev.delta ?? ev.content ?? ev.text);
        if (delta) this.turn().appendThinking(delta);
        break;
      }
      case 'tool_call_start': case 'tool_use': case 'tool_start': case 'tool_call': {
        const line = this.turn().tool(str(ev.id ?? ev.tool_id ?? ev.tool_call_id), str(ev.name ?? ev.tool_name));
        const input = ev.input ?? ev.args ?? ev.tool_args ?? ev.arguments;
        if (input !== undefined && input !== null) line.input = typeof input === 'string' ? parseArgs(input) : input;
        if (typeof ev.display_name === 'string' && ev.display_name) line.displayName = ev.display_name;
        break;
      }
      case 'tool_call_delta': {
        const line = this.turn().tool(str(ev.id ?? ev.tool_id), str(ev.name));
        line.argsText += str(ev.arguments_delta ?? ev.delta);
        break;
      }
      case 'tool_progress': {
        const line = this.turn().findTool(str(ev.id ?? ev.tool_id), '');
        if (line) {
          const total = Number(ev.total);
          const count = Number.isFinite(total) && total > 0 ? `${Number(ev.progress) || 0}/${total}` : '';
          line.progress = [count, str(ev.message)].filter(Boolean).join(' ') || undefined;
        }
        break;
      }
      case 'tool_result': case 'tool_end': {
        const id = str(ev.id ?? ev.tool_id ?? ev.tool_call_id);
        const name = str(ev.name ?? ev.tool_name);
        const line = this.turn().findTool(id, name) ?? this.turn().tool(id, name);
        line.output = ev.output ?? ev.result ?? line.output;
        if (typeof ev.duration_ms === 'number') line.durationMs = ev.duration_ms;
        if (typeof ev.subagent_name === 'string' && ev.subagent_name.trim()) line.displayName = `智能体 · ${ev.subagent_name.trim()}`;
        const failed = !!ev.error || ev.status === 'error';
        if (failed) line.error = errorText(ev.error) || errorText(line.output);
        line.finish(failed ? 'error' : ev.status === 'interrupted' ? 'interrupted' : 'success');
        break;
      }
      case 'subagent_event': {
        const turn = this.turn();
        const parentId = str(ev.parent_tool_id);
        const parentName = str(ev.parent_tool_name) || 'call_subagent';
        const parent = (parentId && turn.findTool(parentId, ''))
          || [...turn.allTools()].reverse().find((t) => t.name === parentName)
          || [...turn.allTools()].reverse().find((t) => t.status === 'running');
        parent?.applySubagent(ev);
        break;
      }
      case 'plan_update': {
        const plan = parsePlan(ev);
        if (plan) signals.push({ type: 'plan', plan });
        break;
      }
      case 'compaction_progress':
        this.turn().compaction(ev);
        break;
      case 'compaction_notice': {
        const c = ev.context_compaction as Record<string, unknown> | undefined;
        const covered = Number(c?.covered_message_count);
        this.turn().addNotice(Number.isFinite(covered) && covered > 0 ? `· 较早的 ${covered} 条消息已在后台压缩` : '· 较早的上下文已在后台压缩');
        usage(c?.context_usage);
        break;
      }
      case 'context_usage':
        usage(ev);
        break;
      case 'meta':
        usage(ev.context_usage);
        break;
      case 'file_confirm': case 'design_pick': {
        const request = parseConfirm(ev);
        if (!request) break;
        if (ev.expired) signals.push({ type: 'confirm_expired', confirmId: request.confirmId });
        else signals.push({ type: 'confirm', request });
        break;
      }
      case 'user_question': {
        const request = parseQuestion(ev);
        if (request) signals.push({ type: 'question', request });
        break;
      }
      case 'user_question_resolved':
        if (ev.request_id) signals.push({ type: 'question_resolved', requestId: str(ev.request_id), outcome: str(ev.outcome) });
        if (ev.outcome === 'timeout') this.turn().addNotice('· 等待回答超时，助手将采用默认方案继续');
        break;
      case 'steer_applied': {
        const message = str(ev.message).trim();
        // The steer splits the reply: freeze this turn, show the user's words, continue in a new one.
        this.endTurn('boundary');
        if (message) this.addUser(message, '插话');
        signals.push({ type: 'steer_applied', message });
        break;
      }
      case 'queued_run_started': {
        const handoff = parseQueuedRun(ev);
        if (handoff) signals.push({ type: 'queued_run', handoff });
        break;
      }
      case 'run_snapshot': {
        // Resume: install what the run produced so far, then keep following.
        const state = (ev.state ?? {}) as Record<string, unknown>;
        const turn = this.turn();
        const thinking = str(state.thinking);
        if (thinking) turn.appendThinking(thinking);
        const tools = Array.isArray(state.tool_calls) ? (state.tool_calls as Record<string, unknown>[]) : [];
        for (const t of tools) {
          const line = turn.tool(str(t.id), str(t.name));
          line.input = t.input ?? t.args;
          if (t.status && t.status !== 'running') {
            line.output = t.output;
            line.finish(t.status === 'error' ? 'error' : 'success');
          }
        }
        const content = str(state.content ?? state.text ?? state.full);
        if (content) turn.replaceText(content);
        break;
      }
      case 'error': {
        const message = errorText(ev.error) || errorText(ev.message) || '运行出错';
        if (!this.cancelling) this.turn().addNotice('✗ ' + message, theme.error);
        signals.push({ type: 'error', message });
        break;
      }
      case 'end': case 'run_terminal':
        signals.push({ type: 'end' });
        break;
      default:
        break;
    }
    return signals;
  }

  /** Render stored messages (oldest first) as finished turns. */
  renderHistory(messages: ChatHistoryMessage[]): void {
    for (const m of messages) {
      if (m.role === 'user') {
        this.addUser(str(m.content));
        continue;
      }
      if (m.role !== 'assistant') continue;
      this.endTurn();
      const turn = new AssistantTurn(this.view);
      this.current = turn;
      this.add(turn);
      const thinking = Array.isArray(m.thinking)
        ? (m.thinking as unknown[]).map((b) => (typeof b === 'string' ? b : str((b as Record<string, unknown>)?.content ?? (b as Record<string, unknown>)?.text))).join('\n')
        : str(m.thinking);
      if (thinking.trim()) {
        const block = new ThinkingBlock(this.view, 0, 0);
        block.append(thinking);
        block.end(0);
        turn.segments.push(block);
      }
      const tools = Array.isArray(m.tool_calls) ? (m.tool_calls as Record<string, unknown>[]) : [];
      // Stored calls use the event field names (tool_name, tool_args, tool_id), with older aliases.
      const addTool = (raw: Record<string, unknown>) => {
        const line = turn.tool(str(raw.tool_id ?? raw.id), str(raw.tool_name ?? raw.name));
        line.input = raw.tool_args ?? raw.input ?? raw.args ?? raw.arguments;
        line.output = raw.output ?? raw.result ?? raw.summary;
        if (typeof raw.duration_ms === 'number') line.durationMs = raw.duration_ms;
        const status = str(raw.status);
        if (status === 'error') line.error = errorText(raw.error) || errorText(line.output);
        line.finish(status === 'error' ? 'error' : status === 'interrupted' ? 'interrupted' : 'success');
        if (line.durationMs === undefined || line.durationMs < 5) line.durationMs = undefined;
      };
      const appendText = (text: string) => {
        const clean = text.replace(/<think>[\s\S]*?<\/think>/g, '');
        if (clean.trim()) turn.appendText(clean);
      };
      // metadata.segments keeps text and tool calls in the order they streamed;
      // without it, fall back to all tools first, then the whole answer.
      const segments = (m.metadata as Record<string, unknown> | undefined)?.segments;
      if (Array.isArray(segments) && segments.length) {
        const shown = new Set<number>();
        for (const segment of segments as Record<string, unknown>[]) {
          if (segment.type === 'text') appendText(str(segment.text));
          else if (segment.type === 'tool' && typeof segment.index === 'number' && tools[segment.index]) {
            shown.add(segment.index);
            addTool(tools[segment.index]);
          }
        }
        tools.forEach((raw, index) => { if (!shown.has(index)) addTool(raw); });
      } else {
        tools.forEach(addTool);
        appendText(str(m.content));
      }
      if (m.error) turn.addNotice('✗ ' + errorText(m.error), theme.error);
      this.endTurn();
    }
  }
}
