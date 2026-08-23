/**
 * The single prop contract every view in the material library implements.
 *
 * Adding a new view kind means: write a component taking `ViewProps`, register
 * it in `registry.tsx`, and add its name to the backend's `VIEW_KINDS`. Nothing
 * else in the host needs to change — that is the extensibility the library is
 * built for.
 */

import type { ReactNode } from 'react';
import type { ViewAction, ViewKind, ViewMap } from './types';

/** Result of running one action: the payload plus the view that should show it. */
export interface ActionOutcome {
  action: ViewAction;
  status: 'loading' | 'success' | 'error';
  data?: unknown;
  error?: string;
  page: number;
  filters: Record<string, string[]>;
}

export interface ViewContext {
  /** Plugin that contributed this view — the data-proxy calls are scoped to it. */
  slug: string;
  /** Tool whose output is being rendered (absent for canvas-only views). */
  toolName?: string;
  /** Full unwrapped payload, the target of `$root.` pointers. */
  root: unknown;
  /** Canvas/view options declared in the manifest. */
  options?: Record<string, string | number | boolean>;
  /** The contribution's own title (canvas title / view label), already resolved. */
  viewTitle?: string;
  /** Report a better title parsed out of the payload (updates the canvas tab). */
  setTitle?: (title: string) => void;
  /** Open the host's detail modal. */
  openDetail?: (title: string, body: ReactNode) => void;
  /** Run a declared action; the host handles the proxy call and paging. */
  runAction?: (
    action: ViewAction,
    scopes: { node?: unknown; item?: unknown },
    opts?: { page?: number; filters?: Record<string, string[]> },
  ) => void;
  /** Outcome of the action currently being displayed, if any. */
  outcome?: ActionOutcome | null;
  /** Dismiss the current action outcome (views that render it in their own chrome). */
  closeOutcome?: () => void;
  /** Render a nested view spec (used by `tabs` and by action results). */
  renderChild?: (spec: { view: ViewKind; map?: ViewMap; actions?: ViewAction[] }, data: unknown) => ReactNode;
}

export interface ViewProps {
  /** Business payload after `unwrap`. */
  data: unknown;
  /** Field mapping from the manifest. */
  map: ViewMap;
  /** Actions declared on this view. */
  actions?: ViewAction[];
  ctx: ViewContext;
}

/** Actions whose trigger matches, filtered by the current tool. */
export function usableActions(
  actions: ViewAction[] | undefined,
  trigger: ViewAction['trigger'],
  toolName?: string,
): ViewAction[] {
  return (actions || []).filter((action) => {
    if (action.trigger !== trigger) return false;
    if (!action.enabled_for_tools?.length) return true;
    return !!toolName && action.enabled_for_tools.includes(toolName);
  });
}
