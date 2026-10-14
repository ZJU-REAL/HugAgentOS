export function projectApiExample(endpoint: string, projectId: string, stream: boolean, chatId = 'project-api-demo') {
  const quote = (value: string) => "'" + value.replaceAll("'", "'\\''") + "'";
  const payload = {
    chat_id: chatId,
    project_id: projectId,
    message: 'Hello',
    stream,
  };
  return [
    `curl${stream ? ' -N' : ''} -X POST ${quote(endpoint)}`,
    "  -H 'Authorization: Bearer <YOUR_PROJECT_API_KEY>'",
    "  -H 'Content-Type: application/json'",
    `  -d ${quote(JSON.stringify(payload, null, 2))}`,
  ].join(' \\\n');
}
