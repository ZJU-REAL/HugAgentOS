import { ChatClient, type ChatEvent, type QuestionAnswer } from '@hugagent/chat-sdk';

/** Deliberately uses DOM textContent: server/model text must never become executable HTML. */
export class Controls {
  private readonly pending = new Map<string, HTMLElement>();
  constructor(private readonly client: ChatClient, private readonly root: HTMLElement,
    private readonly chat: () => string, private readonly error: (message: string) => void) {}
  clear() { this.pending.clear(); this.root.replaceChildren(); }
  private remove(id: string) { this.pending.get(id)?.remove(); this.pending.delete(id); }
  private panel(id: string, title: string) {
    if (this.pending.has(id)) return null;
    const panel = document.createElement('section'); panel.dataset.request = id;
    const heading = document.createElement('p'); heading.textContent = title; panel.append(heading);
    this.pending.set(id, panel); this.root.append(panel); return panel;
  }
  private button(panel: HTMLElement, label: string, action: () => Promise<unknown>) {
    const button = document.createElement('button'); button.type = 'button'; button.textContent = label;
    button.addEventListener('click', () => {
      const buttons = [...panel.querySelectorAll('button')]; buttons.forEach(item => { item.disabled = true; });
      void action().catch(error => {
        this.error(String(error)); buttons.forEach(item => { item.disabled = false; });
      });
    }); panel.append(button);
  }
  consume(event: ChatEvent) {
    if (event.type === 'file_confirm' || event.type === 'design_pick') {
      const id = String(event.confirm_id || ''); if (!id) return;
      if (event.expired || event.resolved) return this.remove(id);
      const panel = this.panel(id, String(event.message || event.question || event.logical_path || event.op || 'Confirm operation'));
      if (!panel) return;
      const decide = async (decision: 'allow' | 'deny' | 'choice' | 'skip', optionId?: string) => {
        const result = await this.client.confirmFile(this.chat(), id, decision, optionId);
        if (!result.ok && !result.stale) throw new Error(result.message || 'Decision was not accepted');
        this.remove(id);
        if (result.stale) this.error(result.message || 'This confirmation has expired');
      };
      if (event.type === 'design_pick') {
        for (const option of Array.isArray(event.options) ? event.options as Record<string, unknown>[] : []) {
          this.button(panel, String(option.title || option.id), () => decide('choice', String(option.id)));
        }
        this.button(panel, 'Skip', () => decide('skip'));
      } else {
        this.button(panel, 'Allow', () => decide('allow')); this.button(panel, 'Deny', () => decide('deny'));
      }
    }
    if (event.type === 'user_question_resolved') this.remove(String(event.request_id || ''));
    if (event.type !== 'user_question') return;
    const id = String(event.request_id || ''); if (!id) return;
    const panel = this.panel(id, 'Assistant needs your answer'); if (!panel) return;
    const answers: (() => QuestionAnswer)[] = [];
    for (const question of Array.isArray(event.questions) ? event.questions as Record<string, unknown>[] : []) {
      const fieldset = document.createElement('fieldset'), legend = document.createElement('legend');
      legend.textContent = String(question.question || ''); fieldset.append(legend);
      const selections: HTMLInputElement[] = [];
      for (const option of Array.isArray(question.options) ? question.options as Record<string, unknown>[] : []) {
        const label = document.createElement('label'), input = document.createElement('input');
        input.type = question.multi_select ? 'checkbox' : 'radio'; input.name = id + ':' + String(question.id);
        input.value = String(option.id); label.append(input, document.createTextNode(String(option.label))); fieldset.append(label); selections.push(input);
      }
      const custom = document.createElement('input'); custom.setAttribute('aria-label', 'Custom answer'); fieldset.append(custom);
      panel.append(fieldset);
      answers.push(() => ({ id: String(question.id), selected: selections.filter(item => item.checked).map(item => item.value), custom: custom.value }));
    }
    this.button(panel, 'Submit answers', async () => {
      const result = await this.client.answerQuestion(this.chat(), id, answers.map(read => read()));
      if (!result.ok && !result.stale) throw new Error(result.message || 'Answer was not accepted');
      this.remove(id); // A successful POST atomically claims and ends the pending request.
      if (result.stale) this.error(result.message || 'This question has expired');
    });
  }
  async recover() {
    const [confirms, questions] = await Promise.all([this.client.pendingConfirms(this.chat()), this.client.pendingQuestions(this.chat())]);
    for (const event of confirms.pendings || []) this.consume({ ...event, type: event.kind === 'design_pick' ? 'design_pick' : 'file_confirm' });
    for (const event of questions.requests || []) this.consume({ ...event, type: 'user_question' });
  }
}
