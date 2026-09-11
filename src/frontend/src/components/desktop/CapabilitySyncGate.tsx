import { useEffect, useState } from 'react';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { desktopCapabilityRequest, type CapabilitySyncStatus } from './capabilitySyncApi';
import './capabilitySync.css';

const names: Record<string, string> = { skill: '技能', agent: '智能体', plugin: '插件' };

/** A blocking login step: the workspace is rendered only after a deliberate outcome. */
export function CapabilitySyncGate() {
  const identityReady = useDeploymentModeStore((s) => s.localReady);
  const modelsReady = useDeploymentModeStore((s) => s.modelsReady);
  const runtimeReady = useDeploymentModeStore((s) => s.capabilitiesReady);
  const bridgeError = useDeploymentModeStore((s) => s.capabilitySyncError);
  const service = useDeploymentModeStore((s) => s.localService);
  const bridgeRetrying = useDeploymentModeStore((s) => s.capabilitySyncRetrying);
  const [status, setStatus] = useState<CapabilitySyncStatus | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [choosing, setChoosing] = useState(false);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      if (!active) return;
      if (identityReady) {
        try {
          const next = await desktopCapabilityRequest<CapabilitySyncStatus>('sync-status');
          if (!active) return;
          setStatus(next);
          setRequestError(null);
          if (next.ready && modelsReady && runtimeReady) {
            useDeploymentModeStore.setState({ capabilityGateOpen: false, partialCapabilities: next.partial });
            return;
          }
        } catch (error) {
          if (active) setRequestError(error instanceof Error ? error.message : '无法读取同步进度');
        }
      }
      if (active) timer = setTimeout(poll, 700);
    }
    void poll();
    return () => { active = false; clearTimeout(timer); };
  }, [identityReady, modelsReady, runtimeReady]);

  // 本机服务自己没起来时，它的状态才是真原因；等待期间不算失败，只报进度。
  const serviceFailed = !modelsReady && service?.phase === 'error';
  const waitingForService = !modelsReady && !!service && !service.ready && !serviceFailed;
  const error = requestError || (serviceFailed ? service.message : null)
    || (!modelsReady ? bridgeError : null) || (!status?.syncing ? status?.error : null);
  // 壳还在自动重试的错误不画成失败：那张卡片上的按钮此时全是灰的，等于没有出路。
  const retrying = bridgeRetrying && !serviceFailed && !requestError;
  const failed = !!error && !choosing && !retrying;
  const fraction = status?.totals_known && status.total > 0 ? status.completed / status.total : undefined;
  async function choose(action: 'retry' | 'continue') {
    setChoosing(true);
    setRequestError(null);
    try {
      await desktopCapabilityRequest(`sync/${action}`, 'POST');
      setStatus(null);
    } catch (problem) {
      setRequestError(problem instanceof Error ? problem.message : '操作未完成，请重试');
    } finally { setChoosing(false); }
  }

  // 本机服务没起来时能力同步接口本身就打不通，重试要落到装/起本机服务上。
  async function restartService() {
    setChoosing(true);
    setRequestError(null);
    try {
      const response = await fetch('/__desktop/setup/install', { method: 'POST' });
      if (!response.ok) throw new Error('本机服务启动请求未被接受');
    } catch (problem) {
      setRequestError(problem instanceof Error ? problem.message : '操作未完成，请重试');
    } finally { setChoosing(false); }
  }

  return (
    <main className="jx-capSyncScreen">
      <section className="jx-capSyncCard" aria-labelledby="cap-sync-title" aria-busy={!failed}>
        <div className="jx-capSyncIcon" aria-hidden="true">
          <svg viewBox="0 0 32 32" fill="none"><path d="m16 4 12 7-12 7L4 11 16 4Zm-12 13 12 7 12-7M4 23l12 7 12-7" stroke="currentColor" strokeWidth="2" strokeLinejoin="round"/></svg>
        </div>
        <h1 id="cap-sync-title">{failed ? '能力同步未完成' : '能力同步中'}</h1>
        <p className="jx-capSyncIntro">{failed
          ? '部分能力暂时未能同步。您可以重试，或仅使用已经同步并验证可用的能力。'
          : '正在准备您的模型、工具与技能。完成后会自动进入工作台。'}</p>
        <div className="jx-capSyncRows" aria-live="polite">
          <div><span>模型与工具清单</span><span>{modelsReady ? '已准备' : waitingForService ? service.message : '连接中'}</span></div>
          {['skill', 'agent', 'plugin'].map((kind) => {
            const group = status?.groups.find((item) => item.kind === kind);
            return <div key={kind}><span>{names[kind]}</span><span>{group?.manifest_ready ? `${group.completed} / ${group.total}` : '等待清单'}</span></div>;
          })}
        </div>
        {failed && <p className="jx-capSyncError" role="alert">{error}</p>}
        <div className="jx-capSyncFootnote" role="status">{choosing ? '正在处理，请稍候…' : failed
          ? '未同步的能力不会出现在可用清单中。'
          : retrying && error ? `正在自动重试：${error}`
          : status?.total ? `已同步 ${status.completed} 项能力` : '正在连接您的能力空间…'}</div>
        {failed && <div className="jx-capSyncActions">
          {serviceFailed
            ? <button type="button" className="jx-capSyncPrimary" onClick={() => void restartService()} disabled={choosing}>重启本机服务</button>
            : <button type="button" className="jx-capSyncPrimary" onClick={() => void choose('retry')} disabled={!identityReady || choosing}>重试同步</button>}
          <button type="button" onClick={() => void choose('continue')} disabled={!status?.can_continue || !modelsReady || choosing}>使用已同步能力继续</button>
        </div>}
        {/* 无论卡在哪一步，这里始终有一个能点的出路：重新登录会重建整条同步链路。 */}
        {(failed || retrying) && <button type="button" className="jx-capSyncEscape"
          onClick={() => { window.location.href = '/__desktop/open-login'; }}>重新登录</button>}
        <div className="jx-capSyncProgress" role="progressbar" aria-label="能力同步进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={fraction === undefined ? undefined : Math.round(fraction * 100)}>
          <div className={`jx-capSyncFill${!failed && fraction === undefined ? ' jx-capSyncIndeterminate' : ''}`} style={fraction === undefined && !failed ? undefined : { width: `${(fraction ?? 0) * 100}%` }} />
        </div>
      </section>
    </main>
  );
}
