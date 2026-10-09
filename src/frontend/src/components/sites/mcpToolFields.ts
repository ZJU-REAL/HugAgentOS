// Match application_schema.ToolDefinition's public MCP parameter restrictions.
const reserved = new Set([
  'limit', 'offset', 'and', 'as', 'assert', 'async', 'await', 'break', 'class',
  'continue', 'def', 'del', 'elif', 'else', 'except', 'finally', 'for', 'from',
  'global', 'if', 'import', 'in', 'is', 'lambda', 'nonlocal', 'not', 'or', 'pass',
  'raise', 'return', 'try', 'while', 'with', 'yield',
]);

export function isMcpField(name: string): boolean {
  return !reserved.has(name) && !name.startsWith('model_');
}
