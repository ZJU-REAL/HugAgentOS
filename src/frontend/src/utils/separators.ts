/** Parse textarea content of "one separator per line" into an array: split by line, trim leading/trailing whitespace, and drop empty lines. */
export function parseSeparators(raw: string): string[] {
  return raw
    .split('\n')
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}
