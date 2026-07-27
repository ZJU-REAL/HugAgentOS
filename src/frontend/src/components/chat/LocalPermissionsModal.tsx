import { useCallback, useEffect, useState } from 'react';
import {
  addLocalGrant,
  getLocalPolicy,
  listLocalGrants,
  listLocalSnapshots,
  removeLocalGrant,
  rollbackLocalFile,
  setLocalPolicy,
  type LocalDisposition,
  type LocalGrant,
  type LocalPolicy,
  type LocalSnapshotFile,
} from '../../api';

/** Desktop local-permissions panel (ticket #06): manage authorized folders and
 *  danger-command policy. Reachable from the deployment switcher. */

const DISPOSITIONS: { value: LocalDisposition; label: string }[] = [
  { value: 'block', label: '始终拦截' },
  { value: 'confirm', label: '每次确认' },
  { value: 'allow', label: '放行' },
];

const CATEGORIES: { key: keyof LocalPolicy; label: string; def: LocalDisposition }[] = [
  { key: 'out_of_scope', label: '越权路径（授权目录之外）', def: 'confirm' },
  { key: 'delete', label: '删除文件/目录', def: 'confirm' },
  { key: 'system_write', label: '写系统目录', def: 'block' },
  { key: 'network', label: '网络外联下载', def: 'confirm' },
  { key: 'privilege', label: '提权（sudo 等）', def: 'block' },
];

export default function LocalPermissionsModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [grants, setGrants] = useState<LocalGrant[]>([]);
  const [policy, setPolicy] = useState<LocalPolicy>({});
  const [snaps, setSnaps] = useState<LocalSnapshotFile[]>([]);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(() => {
    setLoading(true);
    Promise.all([listLocalGrants(), getLocalPolicy(), listLocalSnapshots()])
      .then(([g, p, s]) => {
        setGrants(g);
        setPolicy(p);
        setSnaps(s);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (open) refresh();
  }, [open, refresh]);

  useEffect(() => {
    if (!open) return;
    const onGrant = (e: Event) => {
      const path = (e as CustomEvent<string>).detail;
      if (!path) return;
      addLocalGrant(path).then(refresh).catch((err) => alert('授权失败：' + (err?.message || err)));
    };
    window.addEventListener('hugagent:grant-folder', onGrant as EventListener);
    return () => window.removeEventListener('hugagent:grant-folder', onGrant as EventListener);
  }, [open, refresh]);

  if (!open) return null;

  const changePolicy = (key: keyof LocalPolicy, value: LocalDisposition) => {
    const next = { ...policy, [key]: value };
    setPolicy(next);
    setLocalPolicy(next).catch((err) => alert('保存策略失败：' + (err?.message || err)));
  };

  return (
    <div className="jx-localPermOverlay" onClick={onClose}>
      <div className="jx-localPermModal" onClick={(e) => e.stopPropagation()}>
        <div className="jx-localPermHead">
          <span>本地权限</span>
          <button type="button" className="jx-localPermClose" onClick={onClose} aria-label="关闭">
            ×
          </button>
        </div>

        <div className="jx-localPermSection">
          <div className="jx-localPermSectionTitle">已授权目录</div>
          <div className="jx-localPermHint">默认工作区内自由；此处的目录额外授权，agent 可在其中操作。</div>
          {grants.length === 0 && !loading && <div className="jx-localPermEmpty">暂无授权目录</div>}
          {grants.map((g) => (
            <div key={g.path} className="jx-localPermRow">
              <span className="jx-localPermPath" title={g.path}>
                {g.path}
              </span>
              <span className="jx-localPermMode">{g.mode === 'read' ? '只读' : '读写'}</span>
              <button
                type="button"
                className="jx-localPermRemove"
                onClick={() => removeLocalGrant(g.path).then(refresh)}
              >
                撤销
              </button>
            </div>
          ))}
          <button
            type="button"
            className="jx-localPermAdd"
            onClick={() => {
              window.location.href = '/__desktop/pick-grant-folder';
            }}
          >
            ＋ 授权一个目录
          </button>
        </div>

        <div className="jx-localPermSection">
          <div className="jx-localPermSectionTitle">可回滚的改动</div>
          <div className="jx-localPermHint">agent 改动本地文件前会自动快照，可回滚到上一版。</div>
          {snaps.length === 0 && <div className="jx-localPermEmpty">暂无可回滚的改动</div>}
          {snaps.map((s) => (
            <div key={s.path} className="jx-localPermRow">
              <span className="jx-localPermPath" title={s.path}>
                {s.path}
              </span>
              <span className="jx-localPermMode">{s.count} 版</span>
              <button
                type="button"
                className="jx-localPermRemove"
                onClick={() =>
                  rollbackLocalFile(s.path)
                    .then(refresh)
                    .catch((err) => alert('回滚失败：' + (err?.message || err)))
                }
              >
                回滚
              </button>
            </div>
          ))}
        </div>

        <div className="jx-localPermSection">
          <div className="jx-localPermSectionTitle">危险命令策略</div>
          {CATEGORIES.map((c) => (
            <div key={c.key} className="jx-localPermPolicyRow">
              <span className="jx-localPermPolicyLabel">{c.label}</span>
              <select
                className="jx-localPermSelect"
                value={(policy[c.key] as LocalDisposition) || c.def}
                onChange={(e) => changePolicy(c.key, e.target.value as LocalDisposition)}
              >
                {DISPOSITIONS.map((d) => (
                  <option key={d.value} value={d.value}>
                    {d.label}
                  </option>
                ))}
              </select>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
