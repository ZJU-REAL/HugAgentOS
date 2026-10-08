import { resourceBinding, resourceAssetUrl } from './resource';
/**
 * Host for an L2 plugin module.
 *
 * The module's assets live inside the plugin package and are served from
 * `/v1/plugins/{slug}/web/…`; none of that code is part of the host frontend
 * build. It runs in a sandboxed iframe with `allow-scripts` but deliberately
 * **without** `allow-same-origin`, so the frame is a null origin: inline script
 * cannot read the host's cookies or localStorage, and cannot call `/api` with
 * the user's session. The same posture the artifact preview already uses.
 *
 * If the module fails to load or never completes the handshake, the view falls
 * back to whatever L0 spec the manifest declared — a broken module degrades to
 * a plain card instead of a blank panel.
 */

import { useEffect, useEffectEvent, useMemo, useRef, useState } from 'react';

import { pluginWebAssetUrl } from '../../api';
import { getLang, t } from '../../i18n';
import { useUIStore } from '../../stores';
import { systemPrefersDark } from '../../theme';
import { PluginView } from '../PluginView';
import { resolveText } from '../i18n';
import type { ModuleContribution } from '../types';
import { attachBridge } from './bridge';

/** A module that has not said `module:ready` by now is treated as failed. */
const HANDSHAKE_TIMEOUT_MS = 8000;

export interface PluginModuleFrameProps {
  slug: string;
  module: ModuleContribution;
  /** Payload handed to the module at handshake (usually the tool output). */
  payload: unknown;
  toolName?: string;
  canvasHeaderInset?: number;
  onNewTab?: () => void;
  onNavigateReady?: (navigate: ((url: string) => Promise<void>) | null) => void;
  onOpenCanvas?: (canvasId: string) => void;
  onChatSend?: (text: string) => void;
}

export function PluginModuleFrame(props: PluginModuleFrameProps) {
  const binding = resourceBinding(props.payload, props.module);
  const identity = [props.slug, props.module.id, props.module.entry, binding?.resource_id,
    binding?.revision, binding?.install_id, binding?.execution_scope];
  return <ModuleFrameContent key={JSON.stringify(identity)} {...props} />;
}

function ModuleFrameContent({
  slug,
  module,
  payload,
  toolName,
  onOpenCanvas,
  onChatSend,
  onNewTab,
  onNavigateReady,
  canvasHeaderInset = 0,
}: PluginModuleFrameProps) {
  const frameRef = useRef<HTMLIFrameElement>(null);
  const [status, setStatus] = useState<'loading' | 'ready' | 'failed'>('loading');
  const [autoHeight, setAutoHeight] = useState<number | null>(null);
  const themeMode = useUIStore((state) => state.themeMode);

  // Callbacks live in refs so an inline arrow from a caller cannot re-key the
  // bridge effect: re-attaching also resets `status`, which would re-render,
  // hand us a fresh arrow, and spin.
  const handlersRef = useRef({ onOpenCanvas, onChatSend, onNewTab, onNavigateReady });
  useEffect(() => { handlersRef.current = { onOpenCanvas, onChatSend, onNewTab, onNavigateReady }; }, [onOpenCanvas, onChatSend, onNewTab, onNavigateReady]);

  const bindingKey = JSON.stringify(resourceBinding(payload, module));
  const binding = useMemo(() => JSON.parse(bindingKey) as ReturnType<typeof resourceBinding>, [bindingKey]);
  const [assetSrc, setAssetSrc] = useState<string | null>(null);
  useEffect(() => {
    if (!binding) return;
    const controller = new AbortController();
    void resourceAssetUrl(binding, controller.signal).then(setAssetSrc).catch(() => { if (!controller.signal.aborted) setStatus("failed"); });
    return () => controller.abort();
  }, [binding]);
  const ordinarySrc = useMemo(() => {
    const url = new URL(pluginWebAssetUrl(slug, module.entry), location.href);
    return url.href;
  }, [slug, module.entry]);
  const src = binding ? assetSrc : ordinarySrc;
  const bridgeRef = useRef<ReturnType<typeof attachBridge> | null>(null);
  const isDark = themeMode === 'dark' || (themeMode === 'system' && systemPrefersDark());

  const initializeBridge = useEffectEvent((iframe: HTMLIFrameElement) => attachBridge(iframe, {
      slug,
      grants: module.grants || [],
      resource: binding,
      payload,
      theme: isDark ? 'dark' : 'light',
      locale: getLang(),
      canvasHeaderInset,
      onNewTab: () => handlersRef.current.onNewTab?.(),
      onOpenCanvas: (canvasId) => handlersRef.current.onOpenCanvas?.(canvasId),
      onChatSend: (text) => handlersRef.current.onChatSend?.(text),
      onHeight: (height) => setAutoHeight(height),
      onReady: () => setStatus('ready'),
      onError: () => setStatus('failed'),
  }));
  const canNavigate = module.grants.includes('canvas.new_tab');
  const grantsKey = JSON.stringify(module.grants || []);

  useEffect(() => {
    const iframe = frameRef.current;
    if (!iframe || !src) return undefined;

    const detach = initializeBridge(iframe);

    bridgeRef.current = detach;
    if (canNavigate) handlersRef.current.onNavigateReady?.(detach.navigate);
    const timer = window.setTimeout(() => {
      setStatus((current) => (current === 'ready' ? current : 'failed'));
    }, HANDSHAKE_TIMEOUT_MS);

    return () => {
      window.clearTimeout(timer);
      handlersRef.current.onNavigateReady?.(null);
      detach();
      bridgeRef.current = null;
    };
  }, [slug, module.id, binding?.resource_id, src, grantsKey, canNavigate]);
  useEffect(() => {
    bridgeRef.current?.update(payload, isDark ? 'dark' : 'light', canvasHeaderInset);
  }, [payload, isDark, canvasHeaderInset]);

  if (status === 'failed') {
    if (module.fallback?.view) {
      return (
        <div className="jx-pv-moduleFallback">
          <div className="jx-pv-moduleNotice">{t('插件模块未能加载，已回退到基础视图')}</div>
          <PluginView
            slug={slug}
            view={module.fallback.view}
            map={module.fallback.map}
            actions={module.fallback.actions}
            output={payload}
            toolName={toolName}
            onOpenCanvas={onOpenCanvas}
          />
        </div>
      );
    }
    return (
      <div className="jx-pv-error">
        {t('插件模块「{name}」无法加载', { name: resolveText(module.title, module.id) })}
      </div>
    );
  }

  const height = module.height?.mode === 'fixed'
    ? `${module.height.value}px`
    : module.height?.mode === 'ratio'
      ? `${Math.min(1, Math.max(0.1, module.height.value)) * 100}vh`
      : module.height?.mode === 'auto' && autoHeight
        ? `${autoHeight}px`
        : undefined;

  return (
    <div className={`jx-pv-module${module.surface === 'canvas' ? ' is-canvas' : ''}`} style={height ? { height } : undefined}>
      {status === 'loading' && <div className="jx-pv-loading">{t('正在加载插件模块…')}</div>}
      <iframe
        ref={frameRef}
        className="jx-pv-moduleFrame"
        src={src || undefined}
        title={resolveText(module.title, module.id)}
        // No allow-same-origin on purpose: keeps the frame at a null origin so
        // it cannot reach the host's storage or authenticated API.
        sandbox="allow-scripts"
        referrerPolicy="no-referrer"
        loading={module.surface === 'canvas' ? 'eager' : 'lazy'}
      />
    </div>
  );
}
