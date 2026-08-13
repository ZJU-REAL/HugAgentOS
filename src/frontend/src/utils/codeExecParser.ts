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

/**
 * Decode the available prefix of one JSON string field without requiring the
 * outer tool-arguments object to be complete yet.  Function-call argument
 * deltas commonly contain escaped newlines (``\\n``); decoding them here lets
 * a streamed Write/Edit body render as real multi-line code before ToolCallEnd.
 */
function extractPartialJsonString(raw: string, field: string): string | null {
  const match = new RegExp(`"${field}"\\s*:\\s*"`).exec(raw);
  if (!match || match.index === undefined) return null;

  let index = match.index + match[0].length;
  let output = '';
  while (index < raw.length) {
    const char = raw[index];
    if (char === '"') return output;
    if (char !== '\\') {
      output += char;
      index += 1;
      continue;
    }

    // A trailing backslash is an incomplete escape in the current delta. It
    // will be decoded after the next fragment arrives.
    if (index + 1 >= raw.length) return output;
    const escape = raw[index + 1];
    const simpleEscapes: Record<string, string> = {
      '"': '"',
      '\\': '\\',
      '/': '/',
      b: '\b',
      f: '\f',
      n: '\n',
      r: '\r',
      t: '\t',
    };
    if (escape in simpleEscapes) {
      output += simpleEscapes[escape];
      index += 2;
      continue;
    }
    if (escape === 'u') {
      const hex = raw.slice(index + 2, index + 6);
      if (hex.length < 4 || !/^[0-9a-fA-F]{4}$/.test(hex)) return output;
      output += String.fromCharCode(Number.parseInt(hex, 16));
      index += 6;
      continue;
    }

    // Keep an unknown escape readable while the model is still producing an
    // incomplete argument rather than hiding the remainder of the block.
    output += escape;
    index += 2;
  }
  return output;
}

/** Extract a live code/command preview from incomplete function-call JSON. */
export function extractCodeFromStreamingArgs(
  toolName: string,
  argumentsText: string,
): { code: string; language: string } | null {
  if (!argumentsText) return null;

  try {
    const complete = extractCodeFromInput(toolName, JSON.parse(argumentsText));
    if (complete.code) return complete;
  } catch {
    // Expected while arguments are still streaming; decode the target field
    // directly from the incomplete JSON prefix below.
  }

  if (toolName === 'bash') {
    const code = extractPartialJsonString(argumentsText, 'command');
    return code === null ? null : { code, language: 'bash' };
  }
  if (toolName === 'Write' || toolName === 'Edit') {
    const codeField = toolName === 'Write' ? 'content' : 'new_string';
    const code = extractPartialJsonString(argumentsText, codeField);
    if (code === null) return null;
    const filePath = extractPartialJsonString(argumentsText, 'file_path') ?? '';
    return { code, language: langFromPath(filePath) };
  }
  return null;
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
