/**
 * Canvas panel for a plugin-contributed view.
 *
 * Replaces what used to be one hard-coded panel per plugin feature. The panel
 * looks the active tab's declaration up in the contribution registry and hands
 * it to the material library (or, for an L2 contribution, to the sandboxed
 * module frame). It contains no knowledge of any particular plugin.
 */

import { useCallback, useEffect, useState } from 'react';
import { resourceBinding } from '../../plugin-ui/module/resource';
import { t } from '../../i18n';
import { PluginModuleFrame, PluginView, resolveText } from '../../plugin-ui';
import { useCanvasStore } from '../../stores';
import { usePluginUiStore } from '../../stores/pluginUiStore';
import { useCanvasLauncherStore } from './canvasLauncherStore';
import { CanvasTabBar } from './CanvasTabBar';
import { bindCanvasResourceSource, runForCurrentCanvas } from './canvasResourceSource';

export function PluginCanvasPanel() {
  const [headerInset, setHeaderInset] = useState(0);
  const activeTabId = useCanvasStore(s => s.activeTabId);
  const showLauncher = useCanvasLauncherStore(s => s.show);
  const bindNavigation = useCanvasLauncherStore(s => s.bindNavigation);
  const onNavigateReady = useCallback((navigate: ((url: string) => Promise<void>) | null) => {
    if (activeTabId) bindNavigation(activeTabId, navigate);
  }, [activeTabId, bindNavigation]);
  const target = useCanvasStore((state) => state.pluginTarget);
  const openPluginView = useCanvasStore((state) => state.openPluginView);
  const updatePluginView = useCanvasStore((state) => state.updatePluginView);
  const findCanvas = usePluginUiStore((state) => state.findCanvas);
  const findModule = usePluginUiStore((state) => state.findModule);

  const canvas = target ? findCanvas(target.slug, target.canvasId) : null;
  const requestedModule = target && !canvas ? findModule(target.slug, target.canvasId) : null;
  const binding = resourceBinding(target?.output, requestedModule?.contribution);
  // A tool name can be shared by multiple installed sources. Its returned
  // resource chooses the exact registered declaration, never the first name.
  const module = binding ? findModule(binding.slug, binding.module_id) : requestedModule;
  useEffect(() => {
    if (binding && module && target
        && (target.slug !== binding.slug || target.canvasId !== binding.module_id)) {
      bindCanvasResourceSource(binding);
    }
  }, [binding, module, target]);

  const embeddedHeader = module?.contribution.canvas_header === 'module';

  const body = (() => {
    if (!target) return <div className="jx-rightSidebar-empty">{t('暂无可展示内容')}</div>;
    if (target.status === 'loading') {
      return (
        <div className="jx-canvas-loading" aria-busy="true">
          <div className="jx-canvas-spinner" />
          <span>{t('正在获取数据，完成后将在这里自动展开')}</span>
        </div>
      );
    }
    if (target.status === 'error') {
      return <div className="jx-canvas-error">{target.error || t('加载失败')}</div>;
    }
    if (module) {
      return (
        <PluginModuleFrame
          onNewTab={showLauncher}
          onNavigateReady={onNavigateReady}
          slug={module.slug}
          module={module.contribution}
          canvasHeaderInset={embeddedHeader ? headerInset : 0}
          payload={target.output}
          toolName={target.toolName}
          onOpenCanvas={(canvasId) => runForCurrentCanvas(activeTabId, target,
            () => openPluginView({ ...target, slug: module.slug, canvasId }))}
        />
      );
    }
    if (!canvas) {
      // The contributing plugin was disabled or uninstalled while its tab was
      // open — say so instead of rendering an empty shell.
      return <div className="jx-canvas-error">{t('该插件视图已不可用（插件可能已停用或卸载）')}</div>;
    }
    return (
      <PluginView
        slug={target.slug}
        view={canvas.contribution.view}
        map={canvas.contribution.map}
        actions={canvas.contribution.actions}
        options={canvas.contribution.options}
        unwrapKeys={canvas.contribution.unwrap}
        output={target.output}
        toolName={target.toolName}
        viewTitle={resolveText(canvas.contribution.title)}
        onTitle={(parsedTitle) => {
          // 结果里解析出真正的标题后回写页签，别停在画布声明的通用名上
          if (parsedTitle && parsedTitle !== target.title) runForCurrentCanvas(activeTabId, target,
            () => updatePluginView({ title: parsedTitle }));
        }}
        onOpenCanvas={(canvasId) =>
          runForCurrentCanvas(activeTabId, target,
            () => openPluginView({ ...target, canvasId, status: 'success' }))
        }
      />
    );
  })();

  const title = target
    ? target.title || resolveText(canvas?.contribution.title ?? module?.contribution.title) || t('插件视图')
    : t('插件视图');

  return (
    <aside className={'jx-rightSidebar jx-rightSidebar--plugin' + (embeddedHeader ? ' jx-rightSidebar--moduleHeader' : '')} aria-label={title}>
      <CanvasTabBar controlsOnly={embeddedHeader} onWidthChange={setHeaderInset} />
      <div className="jx-rightSidebar-body jx-rightSidebar-body--fill">{body}</div>
    </aside>
  );
}
