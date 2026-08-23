/** Per-item drill-down buttons, shared by every list-shaped view.
 *
 * "Click an item → run its declared action" renders identically in list,
 * ranking, gallery and status-list; keeping the button block here stops the
 * four copies from drifting.
 */

import { resolveText } from '../../i18n';
import type { ViewContext } from '../../ViewProps';
import type { ViewAction } from '../../types';

export function InlineActions({
  actions,
  item,
  ctx,
}: {
  actions: ViewAction[];
  item: Record<string, unknown>;
  ctx: ViewContext;
}) {
  if (actions.length === 0) return null;
  return (
    <>
      {actions.map((action) => (
        <button
          type="button"
          key={action.id}
          className="jx-pv-inlineAction"
          onClick={() => ctx.runAction?.(action, { item })}
        >
          {resolveText(action.label, action.id)}
        </button>
      ))}
    </>
  );
}

/** Ellipsis-clip display text; one truncation rule for every snippet/summary. */
export function clip(text: string, max = 90): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}
