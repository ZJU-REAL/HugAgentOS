/** External pages receive readiness only. They never receive the plugin bridge. */
export interface EmbeddingPolicy {
  origin: string;
  ready: 'integrated' | 'unconfirmed';
  allowForms: boolean;
  originStorage: boolean;
}
export interface ExternalEmbedLaunch {
  applicationKey: string;
  revision: number;
  target: {kind: 'external'; mode: 'embedded'; url: string};
  embedding: EmbeddingPolicy;
}
export const externalPermissionsPolicy = [
  'camera', 'microphone', 'geolocation', 'display-capture', 'fullscreen',
  'clipboard-read', 'clipboard-write', 'payment', 'usb', 'serial', 'hid',
  'bluetooth', 'midi', 'accelerometer', 'gyroscope', 'magnetometer',
  'autoplay', 'encrypted-media', 'picture-in-picture', 'publickey-credentials-get',
].map(feature => feature + " 'none'").join('; ');

export function validateEmbedLaunch(value: ExternalEmbedLaunch, hostOrigin: string): void {
  if (hostOrigin === 'null') throw new Error('当前环境不支持此应用');
  const policy = value.embedding;
  const url = new URL(value.target.url);
  const origin = new URL(policy.origin);
  if (value.target.kind !== 'external' || value.target.mode !== 'embedded' ||
      !['http:', 'https:'].includes(url.protocol) || url.username || url.password ||
      [...value.target.url].some(c => c.charCodeAt(0) <= 32 || c.charCodeAt(0) === 127) ||
      value.target.url.includes('\\') || origin.origin !== policy.origin ||
      url.origin !== policy.origin || origin.hostname === new URL(hostOrigin).hostname ||
      typeof policy.allowForms !== 'boolean' || typeof policy.originStorage !== 'boolean' ||
      !['integrated', 'unconfirmed'].includes(policy.ready) ||
      (policy.ready === 'integrated' && !policy.originStorage)) {
    throw new Error('应用嵌入配置无效');
  }
}

/** Nonce-only trusted carrier. frame-src also rejects redirects to host/other origins.
 * Host HTML must serve frame-ancestors 'self' so nested external frames cannot
 * load a host document and recover its storage. No external content is proxied.
 */
export async function externalCarrierDocument(launch: ExternalEmbedLaunch, hostOrigin: string): Promise<string> {
  validateEmbedLaunch(launch, hostOrigin);
  if (launch.embedding.originStorage) {
    // The browser cannot safely grant a named origin until host documents reject
    // every external ancestor. A missing deployment header fails closed.
    const response = await fetch(window.location.href, {
      credentials:'same-origin', cache:'no-store', redirect:'error',
    });
    const policies = response.headers.get('Content-Security-Policy') || '';
    if (!response.ok || !/(?:^|;)\s*frame-ancestors\s+'(?:self|none)'\s*(?:;|$)/.test(policies))
      throw new Error('宿主未启用安全嵌入隔离，请在浏览器中打开');
  }
  const config = JSON.stringify({url:launch.target.url, ...launch.embedding, hostOrigin})
    .replace(/</g, '\\u003c');
  const script = `
const config = ${config};
const frame = document.createElement('iframe');
frame.title = 'External application';
frame.sandbox = 'allow-scripts' + (config.originStorage ? ' allow-same-origin' : '') +
  (config.allowForms ? ' allow-forms' : '');
frame.allow = ${JSON.stringify(externalPermissionsPolicy)};
frame.referrerPolicy = 'no-referrer';
frame.style.cssText = 'border:0;width:100%;height:100vh';
let port = null, nonce = '', loaded = false;
function challenge() {
  if (!port || !loaded) return;
  nonce = Array.from(crypto.getRandomValues(new Uint8Array(32)), b => b.toString(16).padStart(2,'0')).join('');
  port.postMessage({type:'loading'});
  if (config.ready === 'integrated')
    frame.contentWindow.postMessage({type:'hugagent.external.init', protocolVersion:'1.0.0', nonce}, config.origin);
}
frame.addEventListener('load', () => { loaded = true; challenge(); });
window.addEventListener('message', event => {
  if (event.source === parent && event.origin === config.hostOrigin &&
      event.data?.type === 'hugagent.external.attach' && !port && event.ports.length === 1) {
    port = event.ports[0]; port.start(); challenge(); return;
  }
  if (config.ready !== 'integrated' || !port || !nonce || event.source !== frame.contentWindow ||
      event.origin !== config.origin || event.data?.type !== 'hugagent.external.ready' ||
      event.data.protocolVersion !== '1.0.0' || event.data.nonce !== nonce) return;
  port.postMessage({type:'ready'}); nonce = '';
});
document.body.append(frame);
frame.src = config.url;
`;
  const nonce = btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(32))));
  const escape = (text: string) => text.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
  const csp = "default-src 'none'; script-src 'nonce-" + nonce +
    "'; style-src 'unsafe-inline'; frame-src " + launch.embedding.origin +
    "; base-uri 'none'; form-action 'none'; object-src 'none'";
  return '<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="' +
    escape(csp) + '"><meta name="referrer" content="no-referrer"></head><body style="margin:0"><script nonce="' + nonce + '">' +
    script + '</script></body></html>';
}
