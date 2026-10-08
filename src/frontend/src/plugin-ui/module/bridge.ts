/**
 * postMessage bridge between the host and an L2 plugin module.
 *
 * The module runs in a `sandbox="allow-scripts"` iframe **without**
 * `allow-same-origin`, so it is a null origin: it cannot read the host's
 * cookies or localStorage, and it cannot call `/api` with the user's session.
 * Everything it is allowed to do goes through the methods below, each gated by
 * the `grants` its manifest declared.
 *
 * Because the origin is literally the string `"null"`, `event.origin` is
 * useless for authentication — the host authenticates by object identity
 * (`event.source === iframe.contentWindow`) instead.
 */

import { navigationRequests } from './navigationRequests';
import { callPluginDataSource } from '../../api';
import { createResourceChannel } from './resourceChannel';
import type { ResourceBinding } from './resource';
import { copyToClipboard } from '../../utils/clipboard';

export const BRIDGE_API_VERSION = 2;

/** Methods a module may invoke, subject to its `grants`. */
export type BridgeMethod =
  | 'data.query'
  | 'canvas.open'
  | 'canvas.new_tab'
  | 'chat.send'
  | 'clipboard.write'
  | 'file.save'
  | 'host.info'
  | 'resource.attach'
  | 'resource.command';

export interface BridgeHooks {
  slug: string;
  resource?: ResourceBinding | null;
  grants: string[];
  /** Payload handed to the module at handshake time (the tool output, usually). */
  payload: unknown;
  theme: 'light' | 'dark';
  locale: string;
  canvasHeaderInset?: number;
  onNewTab?: () => void;
  onOpenCanvas?: (canvasId: string) => void;
  onChatSend?: (text: string) => void;
  onHeight?: (height: number) => void;
  onReady?: () => void;
  onError?: (message: string) => void;
}

interface CallMessage {
  type: 'module:call';
  id: string;
  method: BridgeMethod;
  params?: Record<string, unknown>;
}

/** Actions with an outward effect need a recent user gesture inside the module. */
const GESTURE_REQUIRED: BridgeMethod[] = ['chat.send', 'clipboard.write', 'file.save', 'canvas.new_tab'];
const CALL_RATE_LIMIT = 30;
const CALL_RATE_WINDOW_MS = 10_000;

function granted(grants: string[], method: BridgeMethod, params: Record<string, unknown>): boolean {
  if (method === 'host.info') return true;
  if (method === 'data.query') {
    const source = String(params.source_id || params.source || '');
    return !!source && grants.includes(`data_source:${source}`);
  }
  return grants.includes(method);
}

/**
 * Attach the bridge to one iframe. Returns a disposer that also stops the
 * module from being talked to after unmount — the lifecycle rule that keeps a
 * disabled or uninstalled plugin from lingering.
 */
export function attachBridge(iframe: HTMLIFrameElement, hooks: BridgeHooks): (() => void) & { navigate: (url: string) => Promise<void>; update: (payload: unknown, theme: 'light' | 'dark', canvasHeaderInset?: number) => void } {
  let disposed = false;
  let lastGestureAt = 0;
  const callTimes: number[] = [];
  const abort = new AbortController();
  const resource = hooks.resource && hooks.grants.includes('resource.command')
    ? createResourceChannel(hooks.resource, (event) => {
      const data = event as { type: string; url: string };
      if (data.type === 'download' && Date.now() - lastGestureAt <= 5000) {
        const anchor = document.createElement('a');
        anchor.href = data.url;
        anchor.download = '';
        anchor.click();
      }
    }) : null;

  const post = (message: unknown) => {
    if (disposed) return;
    // Targeting "*" is required: a null-origin frame has no addressable origin.
    // Safe here because the frame is sandboxed and carries no secrets.
    iframe.contentWindow?.postMessage(message, '*');
  };

  const navigation = navigationRequests(post);
  const reply = (id: string, ok: boolean, data?: unknown, error?: string) => {
    post({ type: 'host:result', id, ok, ...(ok ? { data } : { error: error || 'error' }) });
  };

  const handle = async (message: CallMessage) => {
    const params = (message.params || {}) as Record<string, unknown>;
    const { method } = message;

    const now = Date.now();
    while (callTimes.length && now - callTimes[0] > CALL_RATE_WINDOW_MS) callTimes.shift();
    if (callTimes.length >= CALL_RATE_LIMIT) {
      reply(message.id, false, undefined, 'rate_limited');
      return;
    }
    callTimes.push(now);

    if (!granted(hooks.grants, method, params)) {
      reply(message.id, false, undefined, 'not_granted');
      return;
    }
    if (GESTURE_REQUIRED.includes(method) && now - lastGestureAt > 5_000) {
      reply(message.id, false, undefined, 'gesture_required');
      return;
    }

    try {
      switch (method) {
        case 'host.info':
          reply(message.id, true, { theme: hooks.theme, locale: hooks.locale, apiVersion: BRIDGE_API_VERSION });
          return;
        case 'resource.attach': {
          if (!resource || !hooks.resource || hooks.resource.slug !== hooks.slug) throw new Error('resource_not_bound');
          const port = await resource.attach();
          if (disposed) { port.close(); return; }
          iframe.contentWindow?.postMessage({ type: 'host:result', id: message.id, ok: true,
            data: { resource: hooks.resource } }, '*', [port]);
          return;
        }
        case 'data.query': {
          const sourceId = String(params.source_id || params.source || '');
          const query = (params.params && typeof params.params === 'object'
            ? params.params
            : {}) as Record<string, unknown>;
          const data = await callPluginDataSource(hooks.slug, sourceId, query, abort.signal);
          reply(message.id, true, data);
          return;
        }
        case 'canvas.new_tab':
          hooks.onNewTab?.();
          reply(message.id, true, {});
          return;
        case 'canvas.open':
          hooks.onOpenCanvas?.(String(params.canvas_id || ''));
          reply(message.id, true, {});
          return;
        case 'chat.send':
          hooks.onChatSend?.(String(params.text || '').slice(0, 2000));
          reply(message.id, true, {});
          return;
        case 'clipboard.write': {
          // copyToClipboard falls back to execCommand on HTTP intranet deploys,
          // where navigator.clipboard does not exist.
          const copied = await copyToClipboard(String(params.text || '').slice(0, 20_000));
          reply(message.id, copied, copied ? {} : undefined, copied ? undefined : 'copy_failed');
          return;
        }
        case 'file.save': {
          // Handed to the browser as a blob the user explicitly asked for; the
          // module never gets access to the host's file APIs directly.
          const name = String(params.name || 'plugin-file');
          const content = String(params.content || '');
          const blob = new Blob([content], { type: String(params.mime || 'text/plain') });
          const url = URL.createObjectURL(blob);
          const anchor = document.createElement('a');
          anchor.href = url;
          anchor.download = name;
          anchor.click();
          URL.revokeObjectURL(url);
          reply(message.id, true, {});
          return;
        }
        default:
          reply(message.id, false, undefined, 'unknown_method');
      }
    } catch (error) {
      reply(message.id, false, undefined, error instanceof Error ? error.message : 'error');
    }
  };

  const onMessage = (event: MessageEvent) => {
    // Identity check, not origin check: a sandboxed frame reports origin "null".
    if (disposed || event.source !== iframe.contentWindow) return;
    const data = event.data as Record<string, unknown> | null;
    if (!data || typeof data !== 'object') return;

    if (data.type === 'module:navigation-result') { navigation.receive(data); return; }
    if (data.type === 'module:ready') {
      if (typeof data.apiVersion === 'number' && data.apiVersion > BRIDGE_API_VERSION) {
        hooks.onError?.('module_api_too_new');
        return;
      }
      hooks.onReady?.();
      return;
    }
    if (data.type === 'module:gesture') {
      lastGestureAt = Date.now();
      return;
    }
    if (data.type === 'module:resize') {
      const height = Number(data.height);
      if (Number.isFinite(height) && height > 0) hooks.onHeight?.(Math.min(4000, height));
      return;
    }
    if (data.type === 'module:call' && typeof data.id === 'string' && typeof data.method === 'string') {
      void handle(data as unknown as CallMessage);
    }
  };

  window.addEventListener('message', onMessage);

  const sendInit = () => {
    post({
      type: 'host:init',
      version: BRIDGE_API_VERSION,
      theme: hooks.theme,
      locale: hooks.locale,
      canvasHeaderInset: hooks.canvasHeaderInset || 0,
      grants: hooks.grants,
      payload: hooks.payload,
    });
  };
  iframe.addEventListener('load', sendInit);

  const dispose = () => {
    disposed = true;
    abort.abort();
    navigation.dispose();
    resource?.dispose();
    window.removeEventListener('message', onMessage);
    iframe.removeEventListener('load', sendInit);
  };
  return Object.assign(dispose, { navigate: navigation.navigate, update(payload: unknown, theme: 'light' | 'dark', canvasHeaderInset = 0) {
    hooks.payload = payload;
    hooks.theme = theme;
    hooks.canvasHeaderInset = canvasHeaderInset;
    post({ type: 'host:update', payload, theme, canvasHeaderInset });
  }});
}
