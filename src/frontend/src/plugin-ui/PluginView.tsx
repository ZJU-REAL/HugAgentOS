/**
 * Renders one contributed view spec, and owns the action lifecycle around it.
 *
 * The views themselves stay pure: they read mapped fields and call
 * `ctx.runAction`. Everything stateful — calling the L1 proxy, tracking
 * loading/error, paging, filters, nesting a result view — lives here so that
 * adding a new view kind never means re-implementing that plumbing.
 */

import { ApartmentOutlined, ArrowRightOutlined } from '@ant-design/icons';
import { memo, useCallback, useMemo, useRef, useState, type ReactNode } from 'react';

import { callPluginDataSource } from '../api';
import { t } from '../i18n';
import { useUIStore } from '../stores';
import { resolveText } from './i18n';
import { resolveParams, unwrap } from './pointer';
import { getViewEntry } from './registry';
import type { ActionOutcome, ViewContext } from './ViewProps';
import type { ViewAction, ViewKind, ViewMap } from './types';

const PAGE_SIZE = 10;

// Stable identity for "no map declared" — a fresh `{}` per render would
// invalidate every child memo keyed on `map` (the tree canvas re-lays-out).
const EMPTY_MAP: ViewMap = {};

export interface PluginViewProps {
  slug: string;
  view: ViewKind;
  map?: ViewMap;
  actions?: ViewAction[];
  /** Raw tool output; `unwrap` keys are applied before mapping. */
  output: unknown;
  unwrapKeys?: string[];
  toolName?: string;
  options?: Record<string, string | number | boolean>;
  /** The contribution's resolved title, for views that render their own header. */
  viewTitle?: string;
  /** Called when the view parses a better title out of the payload (tab rename). */
  onTitle?: (title: string) => void;
  onOpenCanvas?: (canvasId: string) => void;
  /** Declared `primary_action`: a card that opens a contributed canvas view. */
  primaryAction?: { open_canvas: string };
  /** Card title; defaults to the canvas view's own title. */
  primaryActionLabel?: string;
  /** Card subtitle, worded by the plugin. */
  primaryActionSublabel?: string;
}

function PluginViewInner({
  slug,
  view,
  map,
  actions,
  output,
  unwrapKeys,
  toolName,
  options,
  viewTitle,
  onTitle,
  onOpenCanvas,
  primaryAction,
  primaryActionLabel,
  primaryActionSublabel,
}: PluginViewProps) {
  const setDetailModal = useUIStore((state) => state.setDetailModal);
  const [outcome, setOutcome] = useState<ActionOutcome | null>(null);
  const requestRef = useRef<AbortController | null>(null);

  const data = useMemo(() => unwrap(output, unwrapKeys), [output, unwrapKeys]);
  const entry = getViewEntry(view);

  const closeOutcome = useCallback(() => {
    requestRef.current?.abort();
    setOutcome(null);
  }, []);

  const runAction = useCallback<NonNullable<ViewContext['runAction']>>(
    (action, scopes, opts) => {
      const page = opts?.page ?? 1;
      const filters = opts?.filters ?? {};
      const paging = action.result?.paged
        ? {
            [action.result.page_param || 'page']: page,
            [action.result.page_size_param || 'page_size']: action.result.page_size || PAGE_SIZE,
          }
        : {};
      const params = {
        ...resolveParams(action.params, { root: data, node: scopes.node, item: scopes.item }),
        ...paging,
        ...filters,
      };

      requestRef.current?.abort();
      const controller = new AbortController();
      requestRef.current = controller;
      setOutcome({ action, status: 'loading', page, filters });

      callPluginDataSource(slug, action.data_source, params, controller.signal)
        .then((result) => {
          if (controller.signal.aborted) return;
          setOutcome({ action, status: 'success', data: result, page, filters });
        })
        .catch((error: unknown) => {
          if (controller.signal.aborted) return;
          const message = error instanceof Error ? error.message : String(error);
          setOutcome({ action, status: 'error', error: message, page, filters });
        });
    },
    [data, slug],
  );

  const renderChild = useCallback<NonNullable<ViewContext['renderChild']>>(
    (spec, childData) => (
      <PluginView
        slug={slug}
        view={spec.view}
        map={spec.map}
        actions={spec.actions}
        output={childData}
        toolName={toolName}
        options={options}
        onOpenCanvas={onOpenCanvas}
      />
    ),
    [slug, toolName, options, onOpenCanvas],
  );

  const ctx: ViewContext = useMemo(() => ({
    slug,
    toolName,
    root: data,
    options,
    viewTitle,
    setTitle: onTitle,
    openDetail: (title, body) => setDetailModal({ title, body }),
    runAction,
    closeOutcome,
    outcome,
    renderChild,
  }), [slug, toolName, data, options, viewTitle, onTitle, setDetailModal, runAction, closeOutcome, outcome, renderChild]);

  if (!entry) {
    // A manifest naming a view this build does not have: say so plainly rather
    // than rendering nothing.
    return <div className="jx-pv-error">{t('该插件声明的展示类型不受支持：{view}', { view })}</div>;
  }

  const Component = entry.component;
  const body = (
    <>
      {primaryAction && onOpenCanvas && (
        <button
          type="button"
          className="jx-pv-canvasCard"
          onClick={() => onOpenCanvas(primaryAction.open_canvas)}
        >
          <span className="jx-pv-canvasCardIcon"><ApartmentOutlined /></span>
          <span className="jx-pv-canvasCardText">
            <strong>{primaryActionLabel || t('在画布中查看')}</strong>
            {primaryActionSublabel && <small>{primaryActionSublabel}</small>}
          </span>
          <ArrowRightOutlined className="jx-pv-canvasCardArrow" />
        </button>
      )}
      <Component data={data} map={map || EMPTY_MAP} actions={actions} ctx={ctx} />
    </>
  );

  if (entry.handlesOutcome || !outcome) {
    return <div className={`jx-pv-root${entry.fills ? ' jx-pv-root--fill' : ''}`}>{body}</div>;
  }

  return (
    <div className="jx-pv-root">
      {body}
      <ActionOutcomePanel outcome={outcome} ctx={ctx} onClose={closeOutcome} />
    </div>
  );
}

/**
 * Memoized: the tool card re-renders on every stream tick, and an unchanged
 * plugin view (same output/map identity) must not re-run tree layout or charts.
 */
export const PluginView = memo(PluginViewInner);

function ActionOutcomePanel({
  outcome,
  ctx,
  onClose,
}: {
  outcome: ActionOutcome;
  ctx: ViewContext;
  onClose: () => void;
}): ReactNode {
  const { action } = outcome;
  const paged = !!action.result?.paged;

  return (
    <section className="jx-pv-outcome">
      <header className="jx-pv-outcomeHead">
        <span>{resolveText(action.label, action.id)}</span>
        <button type="button" onClick={onClose}>{t('关闭')}</button>
      </header>
      {outcome.status === 'loading' && <div className="jx-pv-loading">{t('加载中…')}</div>}
      {outcome.status === 'error' && <div className="jx-pv-error">{outcome.error || t('加载失败')}</div>}
      {outcome.status === 'success' && action.result && ctx.renderChild && (
        <>
          {ctx.renderChild({ view: action.result.view, map: action.result.map }, outcome.data)}
          {paged && (
            <div className="jx-pv-pager">
              <button
                type="button"
                disabled={outcome.page <= 1}
                onClick={() => ctx.runAction?.(action, {}, { page: outcome.page - 1, filters: outcome.filters })}
              >
                {t('上一页')}
              </button>
              <span>{outcome.page}</span>
              <button
                type="button"
                onClick={() => ctx.runAction?.(action, {}, { page: outcome.page + 1, filters: outcome.filters })}
              >
                {t('下一页')}
              </button>
            </div>
          )}
        </>
      )}
    </section>
  );
}
