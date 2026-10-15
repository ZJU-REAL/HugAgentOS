import type { ComponentType, ReactNode } from 'react';

/**
 * Canvas views contributed by feature modules that the canvas must not import
 * (for example EE-only modules). A module registers its renderer when it loads;
 * a build without that module never opens such a tab.
 */
export interface CustomCanvasRenderer {
  component: ComponentType;
  icon?: () => ReactNode;
}

const renderers = new Map<string, CustomCanvasRenderer>();

export function registerCanvasRenderer(id: string, renderer: CustomCanvasRenderer) {
  renderers.set(id, renderer);
}

export function canvasRenderer(id: string): CustomCanvasRenderer | undefined {
  return renderers.get(id);
}
