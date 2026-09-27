/** A conversation operation only when it occupies the complete composer text. */
export function classifyForkCommand(input: string): 'fork' | 'invalid' | null {
  const text = input.trim();
  if (text === '/fork') return 'fork';
  return /^\/fork\s/.test(text) ? 'invalid' : null;
}
