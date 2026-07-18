/**
 * Conversion between a tri-state permission switch and `boolean | null`.
 *
 * "Team management" uses `'unset'` to mean "no restriction", "user management" uses `'inherit'` to mean "follow the team default";
 * both correspond to `null` as a value, so they share the same conversion (only the label for null differs).
 */

/** Tri-state string → value: `'on'`→true, `'off'`→false, everything else (unset/inherit)→null. */
export const triToValue = (s: string): boolean | null => (s === 'on' ? true : s === 'off' ? false : null);

/** Value → tri-state string: null→`nullLabel` (default `'unset'`), true→`'on'`, false→`'off'`. */
export const triFromValue = (v: boolean | null, nullLabel = 'unset'): string =>
  v == null ? nullLabel : v ? 'on' : 'off';
