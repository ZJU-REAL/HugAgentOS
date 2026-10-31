import {useEffect, useRef, useState} from 'react';
import {t} from '../i18n';
import {openExternalApplication} from './openExternal';
import {externalCarrierDocument, externalPermissionsPolicy, type ExternalEmbedLaunch} from './externalEmbed';
import {HostButton, HostNotice} from './ui';
import './workspace.css';

const STATUS = {
  loading: {tone: 'loading', text: '正在加载外部页面…'},
  ready: {tone: 'success', text: '外部页面已确认就绪'},
  unconfirmed: {tone: 'warning', text: '未确认就绪。页面可能仍在加载，或不允许嵌入。'},
  invalid: {tone: 'error', text: '应用嵌入配置无效'},
} as const;

function ExternalEmbedCarrier({launch, onRetry}: {
  launch: ExternalEmbedLaunch; onRetry: () => void;
}) {
  const frame = useRef<HTMLIFrameElement>(null);
  const [document, setDocument] = useState('');
  const [status, setStatus] = useState<'loading' | 'ready' | 'unconfirmed' | 'invalid'>('loading');
  const [error, setError] = useState('');
  const connection = useRef<MessageChannel | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fallback = useRef<AbortController | null>(null);
  useEffect(() => {
    let cancelled = false;
    void externalCarrierDocument(launch, window.location.origin).then(html => {
      if (!cancelled) setDocument(html);
    }).catch(e => {if (!cancelled) {setStatus('invalid'); setError(e instanceof Error ? e.message : t('应用嵌入配置无效'));}});
    return () => {
      cancelled = true;
      connection.current?.port1.close(); connection.current?.port2.close();
      connection.current = null;
      if (timer.current) clearTimeout(timer.current);
      fallback.current?.abort();
    };
  }, [launch]);
  function attach() {
    connection.current?.port1.close(); connection.current?.port2.close();
    if (timer.current) clearTimeout(timer.current);
    const channel = new MessageChannel();
    connection.current = channel;
    const loading = () => {
      setStatus('loading');
      if (timer.current) return;
      timer.current = setTimeout(() => setStatus('unconfirmed'), 8000);
    };
    channel.port1.onmessage = event => {
      if (connection.current !== channel) return;
      if (event.data?.type === 'loading') loading();
      if (event.data?.type === 'ready' && launch.embedding.ready === 'integrated') {
        if (timer.current) clearTimeout(timer.current);
        timer.current = null;
        setStatus('ready');
      }
    };
    loading();
    frame.current?.contentWindow?.postMessage({type:'hugagent.external.attach'},
      window.location.origin, [channel.port2]);
  }
  async function browser() {
    fallback.current?.abort();
    const controller = new AbortController(); fallback.current = controller;
    setError('');
    try {await openExternalApplication(launch.applicationKey, controller.signal, true);}
    catch (e) {if (!controller.signal.aborted) setError(e instanceof Error ? e.message : t('打开应用失败'));}
  }
  const current = STATUS[status];
  // The status line is always present (role=status), only its tone changes.
  return <section aria-label={t('外部嵌入工作区')} className="jx-workspaceEmbed">
    <div className="jx-workspaceEmbed-bar">
      <span role="status" className={'jx-workspaceEmbed-status jx-workspaceEmbed-status--' + current.tone}>{t(current.text)}</span>
      <HostButton size="small" onClick={() => void browser()}>{t('在浏览器中打开')}</HostButton>
      <HostButton size="small" tone="text" onClick={onRetry}>{t('重试')}</HostButton>
    </div>
    {error && <HostNotice tone="error">{t(error)}</HostNotice>}
    {document && <iframe ref={frame} srcDoc={document} onLoad={attach} className="jx-workspaceEmbed-frame"
      title={t('外部页面承载器')} sandbox={'allow-scripts allow-same-origin' + (launch.embedding.allowForms ? ' allow-forms' : '')}
      allow={externalPermissionsPolicy} referrerPolicy="no-referrer" />}
  </section>;
}

export function ExternalEmbedFrame(props: {launch:ExternalEmbedLaunch; onRetry:() => void}) {
  // A new launch cannot keep an old iframe or its private message port.
  return <ExternalEmbedCarrier key={JSON.stringify([
    props.launch.applicationKey, props.launch.revision, props.launch.target, props.launch.embedding,
  ])} {...props}/>;
}
