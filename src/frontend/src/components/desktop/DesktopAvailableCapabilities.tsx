import { useEffect, useState } from 'react';
import { useCatalogStore } from '../../stores';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { desktopCapabilityRequest } from './capabilitySyncApi';
import './capabilitySync.css';

type Item = { install_id: string; key?: string; name?: string; runtime_name?: string; display_name?: string; description?: string; enabled?: boolean; usable?: boolean; readiness?: { ready: boolean }; resolution?: { outcome: string } };
const kinds: Record<string, string> = { agents: 'agent', skills: 'skill', mcp: 'mcp', plugins: 'plugin' };
const titles: Record<string, string> = { agents: '智能体', skills: '技能', mcp: '连接器', plugins: '插件' };

export function DesktopAvailableCapabilities() {
  const tab = useCatalogStore((s) => s.abilityTab);
  const [items, setItems] = useState<Item[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    desktopCapabilityRequest<{ items: Item[] }>(`installations?kind=${kinds[tab]}`).then((data) => {
      if (active) setItems(data.items.filter((item) => item.enabled !== false && item.usable !== false
        && item.readiness?.ready && item.resolution?.outcome === 'chosen'));
    }).catch((problem) => { if (active) setError(problem instanceof Error ? problem.message : '读取可用能力失败'); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [tab]);
  async function retry() {
    setRetrying(true);
    try {
      await desktopCapabilityRequest('sync/retry', 'POST');
      useDeploymentModeStore.setState({ capabilityGateOpen: true });
    } catch (problem) { setError(problem instanceof Error ? problem.message : '重试未完成'); }
    finally { setRetrying(false); }
  }
  return <div className="jx-capAvailable">
    <header className="jx-capAvailableHeader"><div><h2>已同步的{titles[tab]}</h2><p>当前仅使用已同步并验证可用的能力。重试成功后会恢复完整清单。</p></div>
      <button type="button" onClick={() => void retry()} disabled={retrying}>重新同步完整能力</button></header>
    {error && <p role="alert">{error}</p>}
    {loading ? <p role="status">正在核验可用能力…</p> : <>
      <p>{items.length} 项可用</p>
      <div className="jx-capAvailableGrid">{items.map((item) => <article key={item.install_id}>
        <h3>{item.display_name || item.name || item.runtime_name || item.key}</h3>
        <p>{item.description || '已同步并通过本机能力校验'}</p>
      </article>)}</div>
      {!items.length && <p>这类能力尚未同步完成，您仍可使用其他已同步能力。</p>}
    </>}
  </div>;
}
