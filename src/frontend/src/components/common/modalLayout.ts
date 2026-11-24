/** Modal props for long forms: centred, and the body scrolls only as a last resort on very
 * short screens, so the footer buttons always stay visible. */
export const COMPACT_MODAL_PROPS = {
  centered: true,
  styles: { body: { maxHeight: 'calc(100vh - 160px)', overflowY: 'auto' as const, overflowX: 'hidden' as const } },
};
