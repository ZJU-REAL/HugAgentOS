/**
 * Extracts code and language from the ToolCall.input of code-type tools (bash / Write / Edit),
 * for use by ToolCallRow's live in-progress code view.
 */
export function extractCodeFromInput(
  toolName: string,
  input: unknown,
): { code: string; language: string } {
  // Handle JSON string input (SSE may deliver args as string)
  let obj: Record<string, unknown> | null = null;
  if (typeof input === 'string') {
    try { obj = JSON.parse(input); } catch { /* not JSON */ }
    if (!obj) return { code: input, language: 'text' };
  } else if (input && typeof input === 'object') {
    obj = input as Record<string, unknown>;
  }
  if (!obj) return { code: '', language: 'text' };

  if (toolName === 'bash') {
    return { code: String(obj.command ?? ''), language: 'bash' };
  }
  if (toolName === 'Write') {
    return {
      code: String(obj.content ?? ''),
      language: langFromPath(String(obj.file_path ?? '')),
    };
  }
  if (toolName === 'Edit') {
    // The new content being written is the meaningful "live" payload.
    return {
      code: String(obj.new_string ?? ''),
      language: langFromPath(String(obj.file_path ?? '')),
    };
  }
  return {
    code: String(obj.code ?? ''),
    language: String(obj.language ?? 'python'),
  };
}

/** Map a file path's extension to a highlight.js language id. */
const _EXT_LANG: Record<string, string> = {
  py: 'python', js: 'javascript', mjs: 'javascript', cjs: 'javascript',
  ts: 'typescript', tsx: 'typescript', jsx: 'javascript',
  sh: 'bash', bash: 'bash', zsh: 'bash',
  json: 'json', yml: 'yaml', yaml: 'yaml', toml: 'ini', ini: 'ini',
  html: 'html', htm: 'html', css: 'css', scss: 'scss', less: 'less',
  md: 'markdown', sql: 'sql', go: 'go', rs: 'rust', java: 'java',
  c: 'c', h: 'c', cpp: 'cpp', cc: 'cpp', xml: 'xml', txt: 'text',
};

export function langFromPath(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase() ?? '';
  return _EXT_LANG[ext] ?? 'text';
}
