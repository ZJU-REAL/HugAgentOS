import { useCallback, useMemo } from 'react';
import { useUIStore } from '../../stores';
import { usePluginUiStore } from '../../stores/pluginUiStore';
import { useCanvasStore } from '../../stores/canvasStore';
import { PluginModuleFrame, PluginView, resolveText } from '../../plugin-ui';
import { renderToolOutputBody } from './ToolOutputRenderer';
export function ToolOutputBody({ toolName, output }: { toolName: string; output: unknown }) {
  const { setDetailModal } = useUIStore();
  const items = usePluginUiStore((state) => state.items);
  const openPluginCanvas = useCanvasStore((state) => state.openPluginView);

  // ToolOutputBody re-renders on every stream tick; memoize the registry scans
  // on the contribution set so an unchanged claim keeps its identity and the
  // (memoized) PluginView subtree doesn't re-render.
  const claim = useMemo(() => {
    const store = usePluginUiStore.getState();
    const module = store.findModuleForTool(toolName);
    if (module && module.contribution.surface === 'tool_view') {
      return { kind: 'module', module } as const;
    }
    const view = store.findToolView(toolName);
    if (!view) return null;
    const primary = view.contribution.primary_action;
    return {
      kind: 'view',
      view,
      // 卡片标题：插件自己的措辞优先，缺省用目标画布的标题
      primaryActionLabel: primary
        ? resolveText(primary.label)
          || resolveText(store.findCanvas(view.slug, primary.open_canvas)?.contribution.title)
        : undefined,
      primaryActionSublabel: primary ? resolveText(primary.sublabel) : undefined,
    } as const;
    // `items` is the store state these lookups read; toolName keys the scan.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [toolName, items]);

  const onOpenCanvas = useCallback(
    (canvasId: string) => {
      const slug = claim?.kind === 'view' ? claim.view.slug : null;
      if (!slug) return;
      const store = usePluginUiStore.getState();
      const module = store.findModule(slug, canvasId)?.contribution;
      if (!store.findCanvas(slug, canvasId) && module?.surface !== 'canvas') return;
      openPluginCanvas({ slug, canvasId, toolName, status: 'success', output });
    },
    [claim, openPluginCanvas, toolName, output],
  );

  if (claim?.kind === 'module') {
    return (
      <PluginModuleFrame
        slug={claim.module.slug}
        module={claim.module.contribution}
        payload={output}
        toolName={toolName}
      />
    );
  }

  if (claim) {
    const { slug, contribution } = claim.view;
    return (
      <PluginView
        slug={slug}
        view={contribution.view}
        map={contribution.map}
        actions={contribution.actions}
        unwrapKeys={contribution.unwrap}
        output={output}
        toolName={toolName}
        primaryAction={contribution.primary_action}
        primaryActionLabel={claim.primaryActionLabel}
        primaryActionSublabel={claim.primaryActionSublabel}
        onOpenCanvas={onOpenCanvas}
      />
    );
  }

  return <>{renderToolOutputBody(toolName, output, setDetailModal)}</>;
}
