/* Grant-scoped messaging client. Credentials remain in the parent page. */
(() => {
  const t = value => window.BrowserText.text(value);
  const pending = new Map();
  let port, epoch, listener, connectionId, initialized = false;
  const id = () => crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2);
  const rpc = (method, params = {}) => new Promise((resolve, reject) => {
    const key = id();
    const timer = setTimeout(() => { pending.delete(key); reject(new Error(t('连接超时'))); }, 35000);
    pending.set(key, { resolve, reject, timer });
    parent.postMessage({ type: 'module:call', id: key, method, params }, '*');
  });
  const receive = (message) => {
    if (message instanceof ArrayBuffer) {
      const length = new DataView(message).getUint32(0);
      const header = JSON.parse(new TextDecoder().decode(message.slice(4, 4 + length)));
      listener?.(header, message.slice(4 + length));
      return;
    }
    if (message.type === 'result') {
      const call = pending.get(message.id);
      if (call) {
        clearTimeout(call.timer);
        pending.delete(message.id);
        message.ok ? call.resolve(message.data) : call.reject(new Error(message.error));
      }
    } else {
      if (message.type === 'attached') connectionId = message.connection_id;
      listener?.(message);
    }
  };
  const layout = message => document.documentElement.style.setProperty('--host-header-inset', Math.max(0, Number(message.canvasHeaderInset) || 0) + 'px');
  window.addEventListener('message', async event => {
    if (event.source !== parent) return;
    const message = event.data;
    if (message.type === 'host:init' && !initialized) {
      initialized = true;
      layout(message);
      document.documentElement.dataset.theme = message.theme;
      parent.postMessage({ type: 'module:ready', apiVersion: 2 }, '*');
      try { await rpc('resource.attach'); } catch (error) { listener?.({ type: 'connection', status: 'error', error: error.message }); }
    } else if (message.type === 'host:navigate') {
      listener?.({ type: 'navigate', id: message.id, url: message.url });
    } else if (message.type === 'host:update') {
      layout(message);
      document.documentElement.dataset.theme = message.theme;
    } else if (message.type === 'host:result') {
      if (event.ports[0]) {
        port = event.ports[0];
        port.onmessage = event => receive(event.data);
        port.start();
        listener?.({ type: 'binding', resource: message.data.resource });
      }
      receive({ ...message, type: 'result' });
    }
  });
  window.addEventListener('pagehide', () => {
    for (const call of pending.values()) { clearTimeout(call.timer); call.reject(new Error(t('浏览器面板已关闭'))); }
    pending.clear();
    port?.close();
  });
  window.addEventListener('click', event => {
    if (event.isTrusted) parent.postMessage({ type: 'module:gesture' }, '*');
  }, true);
  window.BrowserChannel = {
    newTab() { return rpc('canvas.new_tab'); },
    navigationResult(id, error) { parent.postMessage({ type: 'module:navigation-result', id, ok: !error, error: error?.message }, '*'); },
    connection() { return connectionId; },
    listen(fn) { listener = fn; },
    epoch(value) { epoch = value; },
    command(action, params = {}) {
      return new Promise((resolve, reject) => {
        if (!port) { reject(new Error(t('浏览器尚未连接'))); return; }
        const key = id();
        const timer = setTimeout(() => { pending.delete(key); reject(new Error(t('操作超时，结果未知，请先检查页面'))); }, 35000);
        pending.set(key, { resolve, reject, timer });
        port.postMessage({ type: 'command', id: key, action, params, epoch });
      });
    },
    download(download_id) { port?.postMessage({ type: 'download', download_id }); },
  };
})();
