// A fake HugAgentOS for the CLI tests: node:http, the {code,data} envelope, and SSE framed as in
// .claude/skills/hugagent-frontend-dev/references/sse-events.md. The user message picks the script:
//   "fail" → error event; "hang" → run_started then silence; "approve" → file_confirm and wait for the
//   answer; "question" → user_question and wait; "drop" → cut the stream mid-run (resume finishes it).
import { createServer } from 'node:http';

export const TOKEN = 'sk-jx-test-token';

const ok = (data, code = 10000) => ({ code, message: 'Success', data, trace_id: 't', timestamp: 0 });

export async function startFakeServer({ token = TOKEN, port = 0 } = {}) {
  const state = {
    requests: [],
    confirms: [],
    answers: [],
    cancels: [],
    steers: [],
    resumes: [],
    operations: new Map(),
    runStatusCalls: 0,
    waiters: new Map(),
  };
  let offset = 0;
  const sse = (res, event) => {
    offset += 1;
    res.write(`data: ${JSON.stringify({ server_ts: Date.now(), event_offset: offset, ...event })}\n\n`);
  };
  const waitFor = (key) => new Promise((resolve) => state.waiters.set(key, resolve));
  const wake = (key, value) => { state.waiters.get(key)?.(value); state.waiters.delete(key); };

  const server = createServer(async (req, res) => {
    const url = new URL(req.url, 'http://x');
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks).toString();
    const body = raw ? JSON.parse(raw) : undefined;
    state.requests.push({ method: req.method, path: url.pathname, query: Object.fromEntries(url.searchParams), body });
    const json = (status, value) => {
      res.writeHead(status, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(value));
    };
    if (url.pathname === '/health') return json(200, { status: 'ok' });
    if (req.headers.authorization !== 'Bearer ' + token) return json(401, { detail: 'Not authenticated' });

    const route = `${req.method} ${url.pathname}`;
    let match;
    if (route === 'GET /v1/tool-approval') return json(200, ok({ mode: 'ask' }));
    if (route === 'PUT /v1/tool-approval') return json(200, ok({ mode: body.mode }));
    if (route === 'POST /v1/projects/local/resolve') {
      return json(200, ok({ project_id: 'proj1', name: 'repo', local_path: body.path, created: false }));
    }
    if (route === 'POST /v1/chats') {
      return json(201, ok({ chat_id: 'chat' + (state.requests.length + 100), title: body.title ?? '新对话', project_id: body.project_id ?? null }, 10001));
    }
    if (route === 'GET /v1/projects/proj1/chats') {
      return json(200, ok({ items: [
        { chat_id: 'old1', title: 'older', project_id: 'proj1', updated_at: '2026-01-01T00:00:00' },
        { chat_id: 'new1', title: 'newer', project_id: 'proj1', updated_at: '2026-02-01T00:00:00' },
      ] }));
    }
    if (route === 'GET /v1/chats') return json(200, ok({ items: [{ chat_id: 'cloud1', title: 'cloud chat' }] }));
    if ((match = url.pathname.match(/^\/v1\/chats\/([^/]+)\/messages$/))) {
      if (match[1] === 'busy') return json(409, { detail: { code: 'busy', message: 'chat is busy' } });
      // Like the real route: order=desc picks the latest page, but a page stays in conversation order.
      return json(200, ok({ items: [
        { message_id: 'm1', role: 'user', content: 'first', chat_seq: 1 },
        { message_id: 'm2', role: 'assistant', content: 'second', chat_seq: 2 },
      ] }));
    }
    if (url.pathname === '/v1/chats/envelope/messages-bad') return json(200, { code: 20001, message: 'business refused', data: null });
    if (url.pathname === '/v1/chats/validation/fork') {
      return json(422, { detail: [{ loc: ['body', 'request_id'], msg: 'field required' }] });
    }
    if (url.pathname === '/v1/chats/proxy/compact') {
      res.writeHead(502, { 'Content-Type': 'text/html' });
      return res.end('<html>Bad Gateway</html>');
    }
    if ((match = url.pathname.match(/^\/v1\/chats\/([^/]+)\/compact$/))) {
      if (match[1] === 'missing') return json(404, { detail: 'Not Found' });
      return json(200, ok({ compacted: true, tokens_before: 100, tokens_after: 10 }));
    }
    if ((match = url.pathname.match(/^\/v1\/chats\/([^/]+)\/fork$/))) {
      return json(201, ok({ chat_id: match[1] + '-fork', title: 'fork', project_id: 'proj1' }, 10001));
    }
    if ((match = url.pathname.match(/^\/v1\/chats\/([^/]+)\/file-confirm$/))) {
      state.confirms.push(body);
      wake('confirm', body);
      return json(200, ok({ ok: true }));
    }
    if ((match = url.pathname.match(/^\/v1\/chats\/([^/]+)\/user-questions\/([^/]+)\/answer$/))) {
      state.answers.push(body);
      wake('answer', body);
      return json(200, ok({ ok: true }));
    }
    if ((match = url.pathname.match(/^\/v1\/chats\/([^/]+)\/active-run$/))) return json(200, ok(null));
    if ((match = url.pathname.match(/^\/v1\/chat-runs\/([^/]+)\/cancel$/))) {
      state.cancels.push(match[1]);
      wake('cancel', match[1]);
      return json(200, ok({ cancelled: true }));
    }
    if ((match = url.pathname.match(/^\/v1\/chat-runs\/([^/]+)\/steer$/))) {
      state.steers.push(body);
      return json(200, ok({ queued: true }));
    }
    if (route === 'GET /v1/models/capabilities') {
      return json(200, ok({ main_agent: {}, user_model_switch: { enabled: true, models: [
        { provider_id: 'p1', display_name: 'Model One', model_name: 'one', is_default: true },
        { provider_id: 'p2', display_name: '', model_name: 'two' },
      ] } }));
    }
    if ((match = url.pathname.match(/^\/v1\/chats\/stream\/([^/]+)$/))) {
      state.resumes.push({ runId: match[1], from: Number(url.searchParams.get('from')) });
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      sse(res, { type: 'content', delta: ' resumed' });
      sse(res, { type: 'end' });
      return res.end('data: [DONE]\n\n');
    }
    if (route === 'POST /v1/agents/responses') return respond(body, res);

    // session-manager queue (device:<id>)
    if (route === 'GET /v1/personal-agent/session-manager/devices') return json(200, ok([{ id: 'dev1', name: 'laptop' }]));
    if (route === 'POST /v1/personal-agent/session-manager/operations') {
      const id = 'op' + (state.operations.size + 1);
      state.operations.set(id, body);
      return json(202, ok({ id, target: body.target, operation: body.operation, status: 'sent', result: null }));
    }
    if ((match = url.pathname.match(/^\/v1\/personal-agent\/session-manager\/operations\/([^/]+)$/))) {
      const op = state.operations.get(match[1]);
      if (!op) return json(404, { detail: 'Operation not found' });
      return json(200, ok({ id: match[1], status: 'completed', result: deviceResult(op) }));
    }
    return json(404, { detail: 'Not Found' });
  });

  function deviceResult(op) {
    switch (op.operation) {
      case 'chat.create': return { chat_id: 'dchat', title: op.args.title ?? '新对话' };
      case 'chat.send': return { type: 'run_started', run_id: 'drun', message_id: 'dm2' };
      case 'run.status':
        state.runStatusCalls += 1;
        return { run_id: 'drun', chat_id: 'dchat', message_id: 'dm2', status: state.runStatusCalls > 1 ? 'completed' : 'running' };
      case 'chat.history': return { items: [{ message_id: 'dm2', role: 'assistant', content: 'device answer' }] };
      case 'chat.list': return { items: [{ chat_id: 'dchat', title: 'on device' }] };
      case 'run.cancel': return { error: 'request_refused', http_status: 404 };
      default: return {};
    }
  }

  async function respond(body, res) {
    const runId = 'run-' + body.chat_id;
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    res.write(': heartbeat\n\n');
    sse(res, { type: 'run_started', run_id: runId, message_id: 'msg1' });
    const message = body.message;
    if (message === 'hang') {
      await waitFor('cancel');
      sse(res, { type: 'error', error: 'cancelled by user' });
      sse(res, { type: 'end' });
      return res.end('data: [DONE]\n\n');
    }
    if (message === 'drop') {
      sse(res, { type: 'content', delta: 'partial' });
      // Let the frames reach the client before the connection drops.
      return setTimeout(() => res.socket.destroy(), 100);
    }
    if (message === 'fail') {
      sse(res, { type: 'error', error: 'model exploded' });
      sse(res, { type: 'end' });
      return res.end('data: [DONE]\n\n');
    }
    if (message === 'approve') {
      sse(res, { type: 'tool_call_start', id: 't1', name: 'write_file' });
      sse(res, { type: 'tool_call', id: 't1', name: 'write_file', input: { path: 'a.txt' } });
      sse(res, { type: 'file_confirm', confirm_id: 'cf1', path: 'a.txt' });
      const decision = await waitFor('confirm');
      sse(res, { type: 'tool_result', id: 't1', output: decision.decision });
      sse(res, { type: 'content', delta: 'decision=' + decision.decision });
      sse(res, { type: 'end' });
      return res.end('data: [DONE]\n\n');
    }
    if (message === 'question') {
      sse(res, { type: 'user_question', request_id: 'q1', questions: [{ id: 'a', question: 'Which?' }, { id: 'b', question: 'Why?' }] });
      await waitFor('answer');
      sse(res, { type: 'content', delta: 'answered' });
      sse(res, { type: 'end' });
      return res.end('data: [DONE]\n\n');
    }
    sse(res, { type: 'thinking', delta: 'hmm' });
    sse(res, { type: 'content', delta: 'Hello' });
    sse(res, { type: 'tool_call', id: 't1', name: 'list_dir', input: { path: '.' } });
    sse(res, { type: 'tool_result', id: 't1', output: 'a b' });
    sse(res, { type: 'content', delta: ', world' });
    sse(res, { type: 'end' });
    res.end('data: [DONE]\n\n');
  }

  await new Promise((resolve) => server.listen(port, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${server.address().port}`;
  return {
    url,
    state,
    close: () => new Promise((resolve) => { server.closeAllConnections(); server.close(resolve); }),
  };
}
