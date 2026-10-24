import { useCallback, useContext, useEffect } from 'react';
import { create } from 'zustand';
import { useCanvasStore } from '../../stores/canvasStore';
import { CanvasPaneContext } from './CanvasPaneContext';

/** 'fit' = scale content to the pane width; a number is an absolute scale (1 = 100%). */
export type CanvasZoom = 'fit' | number;

export const CANVAS_ZOOM_STEPS = [0.25, 0.33, 0.5, 0.67, 0.75, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 3, 4] as const;
export const CANVAS_ZOOM_MIN = CANVAS_ZOOM_STEPS[0];
export const CANVAS_ZOOM_MAX = CANVAS_ZOOM_STEPS[CANVAS_ZOOM_STEPS.length - 1];

interface CanvasZoomState {
  zoomByTab: Record<string, CanvasZoom>;
  /** Scale the renderer resolved 'fit' to, so the control can show a real percentage and step from it. */
  fitScaleByTab: Record<string, number>;
  /** Mount count of zoom-capable renderers per tab; the header shows its control only while > 0. */
  zoomableByTab: Record<string, number>;
  setZoom: (tabId: string, zoom: CanvasZoom) => void;
  setFitScale: (tabId: string, scale: number) => void;
}

// Shared contract between the header zoom control and every renderer that can
// scale (docx, image, pdf, text). Renderers declare themselves with
// useCanvasZoomTarget() and read their tab's value; only the control writes it.
export const useCanvasZoomStore = create<CanvasZoomState>((set) => ({
  zoomByTab: {},
  fitScaleByTab: {},
  zoomableByTab: {},
  setZoom: (tabId, zoom) => set((s) => ({
    zoomByTab: { ...s.zoomByTab, [tabId]: zoom === 'fit' ? zoom : clampZoom(zoom) },
  })),
  setFitScale: (tabId, scale) => set((s) => (s.fitScaleByTab[tabId] === scale ? {} : {
    fitScaleByTab: { ...s.fitScaleByTab, [tabId]: scale },
  })),
}));

export function useCanvasZoom(tabId: string | null | undefined): CanvasZoom {
  return useCanvasZoomStore((s) => (tabId ? s.zoomByTab[tabId] : undefined) ?? 'fit');
}

export function useCanvasZoomable(tabId: string | null | undefined): boolean {
  return useCanvasZoomStore((s) => !!tabId && (s.zoomableByTab[tabId] ?? 0) > 0);
}

export function clampZoom(scale: number): number {
  return Math.min(CANVAS_ZOOM_MAX, Math.max(CANVAS_ZOOM_MIN, scale));
}

/** The numeric scale a zoom value currently renders at ('fit' resolves to the renderer-reported fit scale). */
export function resolveZoom(tabId: string, zoom: CanvasZoom = useCanvasZoomStore.getState().zoomByTab[tabId] ?? 'fit'): number {
  return zoom === 'fit' ? useCanvasZoomStore.getState().fitScaleByTab[tabId] ?? 1 : zoom;
}

/** Next preset step above (dir 1) or below (dir -1) the tab's current scale. */
export function stepZoom(tabId: string, dir: 1 | -1) {
  const current = resolveZoom(tabId);
  const next = dir > 0
    ? CANVAS_ZOOM_STEPS.find((step) => step > current + 0.001) ?? CANVAS_ZOOM_MAX
    : [...CANVAS_ZOOM_STEPS].reverse().find((step) => step < current - 0.001) ?? CANVAS_ZOOM_MIN;
  useCanvasZoomStore.getState().setZoom(tabId, next);
}

/**
 * Called by a renderer that honours the zoom contract. Registers the pane's
 * current tab as zoomable while mounted (so the header shows the control) and
 * returns that tab's zoom plus a callback to report what 'fit' resolved to.
 */
export function useCanvasZoomTarget(enabled = true) {
  const pane = useContext(CanvasPaneContext);
  const tabId = useCanvasStore((s) => s.paneTabs[pane]);
  const zoom = useCanvasZoom(tabId);
  useEffect(() => {
    if (!enabled || !tabId) return;
    const bump = (delta: number) => useCanvasZoomStore.setState((s) => ({
      zoomableByTab: { ...s.zoomableByTab, [tabId]: (s.zoomableByTab[tabId] ?? 0) + delta },
    }));
    bump(1);
    return () => bump(-1);
  }, [enabled, tabId]);
  const reportFitScale = useCallback((scale: number) => {
    if (tabId) useCanvasZoomStore.getState().setFitScale(tabId, scale);
  }, [tabId]);
  return { tabId, zoom, reportFitScale };
}
