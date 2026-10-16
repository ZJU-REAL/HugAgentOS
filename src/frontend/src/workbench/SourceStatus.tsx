import {t} from '../i18n';
import {sourceLabel,type SourceFailure} from './sourceIdentity';
import {reloadDirectory} from './catalog';

export function SourceStatus({failures}:{failures:SourceFailure[]}) {
  if(!failures.length)return null;
  return <div role="status">{failures.map(failure=><p key={failure.source}>
    {t(sourceLabel({kind:failure.source,id:''}))}: {t(failure.code==='execution_plane_not_ready'
      ? '本机执行面尚未就绪' : failure.code==='origin_mismatch' ? '服务身份已改变，请重新加载' : '此来源暂不可用，请检查连接')}
  </p>)}<button type="button" onClick={()=>void reloadDirectory()}>{t('重试')}</button></div>;
}
