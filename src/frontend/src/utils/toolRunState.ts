/**
 * Tool runs stay open while they are first rendered live. Historical runs are
 * mounted as non-streaming after a refresh, so their details start collapsed.
 */
export function getToolRunInitialOpen(isStreaming?: boolean): boolean {
  return isStreaming === true;
}
