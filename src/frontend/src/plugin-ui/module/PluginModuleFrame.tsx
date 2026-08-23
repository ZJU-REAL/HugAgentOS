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

import { useEffect, useMemo, useRef, useState } from 'react';

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
  onOpenCanvas?: (canvasId: string) => void;
  onChatSend?: (text: string) => void;
}

export function PluginModuleFrame({
  slug,
  module,
  payload,
  toolName,
  onOpenCanvas,
  onChatSend,
}: PluginModuleFrameProps) {
  const frameRef = useRef<HTMLIFrameElement>(null);
  const [status, setStatus] = useState<'loading' | 'ready' | 'failed'>('loading');
  const [autoHeight, setAutoHeight] = useState<number | null>(null);
  const themeMode = useUIStore((state) => state.themeMode);

  // Callbacks live in refs so an inline arrow from a caller cannot re-key the
  // bridge effect: re-attaching also resets `status`, which would re-render,
  // hand us a fresh arrow, and spin.
  const handlersRef = useRef({ onOpenCanvas, onChatSend });
  handlersRef.current = { onOpenCanvas, onChatSend };

  const src = useMemo(() => pluginWebAssetUrl(slug, module.entry), [slug, module.entry]);
  const isDark = themeMode === 'dark' || (themeMode === 'system' && systemPrefersDark());

  useEffect(() => {
    const iframe = frameRef.current;
    if (!iframe) return undefined;
    setStatus('loading');

    const detach = attachBridge(iframe, {
      slug,
      grants: module.grants || [],
      payload,
      theme: isDark ? 'dark' : 'light',
      locale: getLang(),
      onOpenCanvas: (canvasId) => handlersRef.current.onOpenCanvas?.(canvasId),
      onChatSend: (text) => handlersRef.current.onChatSend?.(text),
      onHeight: (height) => setAutoHeight(height),
      onReady: () => setStatus('ready'),
      onError: () => setStatus('failed'),
    });

    const timer = window.setTimeout(() => {
      setStatus((current) => (current === 'ready' ? current : 'failed'));
    }, HANDSHAKE_TIMEOUT_MS);

    return () => {
      window.clearTimeout(timer);
      detach();
    };
  }, [slug, module.grants, payload, isDark]);

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
        src={src}
        title={resolveText(module.title, module.id)}
        // No allow-same-origin on purpose: keeps the frame at a null origin so
        // it cannot reach the host's storage or authenticated API.
        sandbox="allow-scripts"
        loading="lazy"
      />
    </div>
  );
}
