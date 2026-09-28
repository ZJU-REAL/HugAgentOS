export const AGENT_RESPONSES_PATH = '/v1/agents/responses';

export function resolveAgentApiEndpoint(options: {
  origin: string; apiBase: string; isDesktop: boolean; local: boolean;
  serverBase: string; localBase: string;
}): string {
  if (options.isDesktop) {
    const base = options.local ? options.localBase : options.serverBase;
    if (!base) return '';
    return `${base.replace(/\/+$/, '')}${options.local ? '' : '/api'}${AGENT_RESPONSES_PATH}`;
  }
  return new URL(`${options.apiBase.replace(/\/+$/, '')}${AGENT_RESPONSES_PATH}`, options.origin).href;
}

export function agentApiExamples(endpoint: string, stream: boolean, agentId: string) {
  const payload = { agent_id: agentId, chat_id: 'agent-api-demo', message: 'Hello', stream };
  // Example values never contain a real key. JSON keeps user-controlled data out
  // of shell snippets; the endpoint comes from configured deployment origins.
  const quote = (value: string) => "'" + value.replaceAll("'", "'\\''") + "'";
  const curl = [
    `curl${stream ? ' -N' : ''} -X POST ${quote(endpoint)}`,
    "  -H 'Authorization: Bearer <YOUR_AGENT_API_KEY>'",
    "  -H 'Content-Type: application/json'",
    `  -d ${quote(JSON.stringify(payload, null, 2))}`,
  ].join(' \\\n');
  const python = [
    'import os',
    'import requests',
    '',
    `payload = {"agent_id": ${JSON.stringify(agentId)}, "chat_id": "agent-api-demo", "message": "Hello", "stream": ${stream ? 'True' : 'False'}}`,
    `with requests.post(${JSON.stringify(endpoint)},`,
    '    headers={"Authorization": "Bearer " + os.environ["AGENT_API_KEY"]},',
    `    json=payload, stream=${stream ? 'True' : 'False'}, timeout=(10, 600)) as response:`,
    '    response.raise_for_status()',
    ...(stream ? [
      '    for line in response.iter_lines(decode_unicode=True):',
      '        if line:',
      '            print(line)',
    ] : ['    result = response.json()', '    print(result["response"])']),
  ].join('\n');
  return { curl, python, payload };
}
