import {useEffect, useRef, useState} from 'react';
import {t} from '../i18n';
import {openExternalApplication} from './openExternal';
import {externalCarrierDocument, externalPermissionsPolicy, type ExternalEmbedLaunch} from './externalEmbed';

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
  return <section aria-label={t('外部嵌入工作区')} style={{width:'100%', minHeight:400}}>
    <div role="status">
      {status === 'loading' && t('正在加载外部页面…')}
      {status === 'ready' && t('外部页面已确认就绪')}
      {status === 'unconfirmed' && t('未确认就绪。页面可能仍在加载，或不允许嵌入。')}
      {status === 'invalid' && t('应用嵌入配置无效')}
    </div>
    <button type="button" onClick={() => void browser()}>{t('在浏览器中打开')}</button>{' '}
    <button type="button" onClick={onRetry}>{t('重试')}</button>
    {error && <p role="alert">{t(error)}</p>}
    {document && <iframe ref={frame} srcDoc={document} onLoad={attach}
      title={t('外部页面承载器')} sandbox={'allow-scripts allow-same-origin' + (launch.embedding.allowForms ? ' allow-forms' : '')}
      allow={externalPermissionsPolicy} referrerPolicy="no-referrer"
      style={{border:0, width:'100%', height:'70vh', minHeight:400}} />}
  </section>;
}

export function ExternalEmbedFrame(props: {launch:ExternalEmbedLaunch; onRetry:() => void}) {
  // A new launch cannot keep an old iframe or its private message port.
  return <ExternalEmbedCarrier key={JSON.stringify([
    props.launch.applicationKey, props.launch.revision, props.launch.target, props.launch.embedding,
  ])} {...props}/>;
}
