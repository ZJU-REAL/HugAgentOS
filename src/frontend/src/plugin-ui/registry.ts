/**
 * The material library's registry — the one place a view kind is bound to a
 * component.
 *
 * Adding a view kind is a three-line change here plus the component file and
 * the matching entry in the backend's `VIEW_KINDS`. Nothing else in the host
 * refers to view kinds by name, which is what keeps the library extensible
 * without touching the render path.
 */

import type { ComponentType } from 'react';

import type { ViewProps } from './ViewProps';
import type { ViewKind } from './types';

import { BadgeView } from './views/document/BadgeView';
import { KVView } from './views/document/KVView';
import { ListView } from './views/document/ListView';
import { MarkdownView } from './views/document/MarkdownView';
import { MetricsView } from './views/document/MetricsView';
import { SectionsView } from './views/document/SectionsView';
import { TableView } from './views/document/TableView';

import { ComparisonView } from './views/analytic/ComparisonView';
import { DistributionView } from './views/analytic/DistributionView';
import { RankingView } from './views/analytic/RankingView';
import { ScoreView } from './views/analytic/ScoreView';
import { TimelineView } from './views/analytic/TimelineView';
import { TimeseriesView } from './views/analytic/TimeseriesView';

import { GalleryView } from './views/container/GalleryView';
import { LinkCardView } from './views/container/LinkCardView';
import { StatusListView } from './views/container/StatusListView';
import { TabsView } from './views/container/TabsView';
import { TraceView } from './views/container/TraceView';
import { TreeGraphView } from './views/container/TreeGraphView';

export interface ViewEntry {
  component: ComponentType<ViewProps>;
  /**
   * True when the view places an action's result itself (the graph canvas puts
   * it in a side panel). Everything else lets the host render the outcome
   * beneath the view.
   */
  handlesOutcome?: boolean;
  /** Views that want the full height of a canvas rather than card flow. */
  fills?: boolean;
}

export const VIEW_REGISTRY: Record<ViewKind, ViewEntry> = {
  // A — document-shaped
  badge: { component: BadgeView },
  kv: { component: KVView },
  list: { component: ListView },
  table: { component: TableView },
  markdown: { component: MarkdownView },
  sections: { component: SectionsView },
  metrics: { component: MetricsView },

  // B — analytical
  timeseries: { component: TimeseriesView },
  ranking: { component: RankingView },
  comparison: { component: ComparisonView },
  distribution: { component: DistributionView },
  score: { component: ScoreView },
  timeline: { component: TimelineView },

  // C — container / interactive
  'tree-graph': { component: TreeGraphView, handlesOutcome: true, fills: true },
  gallery: { component: GalleryView },
  'status-list': { component: StatusListView },
  trace: { component: TraceView },
  'link-card': { component: LinkCardView },
  tabs: { component: TabsView },
};

export function isViewKind(value: unknown): value is ViewKind {
  return typeof value === 'string' && value in VIEW_REGISTRY;
}

export function getViewEntry(kind: string): ViewEntry | null {
  return isViewKind(kind) ? VIEW_REGISTRY[kind] : null;
}
