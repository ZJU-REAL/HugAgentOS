import { useCallback, useEffect, useState } from 'react';
import { Button, Modal, Select, message } from 'antd';
import { PlusOutlined } from '@ant-design/icons';
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
import { t } from '../../i18n';

/** Desktop local-permissions panel (ticket #06): manage authorized folders and
 *  danger-command policy. Reachable from the deployment switcher. */

const DISPOSITIONS: { value: LocalDisposition; label: string }[] = [
  { value: 'block', label: t('始终拦截') },
  { value: 'confirm', label: t('每次确认') },
  { value: 'allow', label: t('放行') },
];

const GRANT_MODES: { value: 'read' | 'readwrite'; label: string }[] = [
  { value: 'read', label: t('只读') },
  { value: 'readwrite', label: t('读写') },
];

const CATEGORIES: { key: keyof LocalPolicy; label: string; def: LocalDisposition }[] = [
  { key: 'out_of_scope', label: t('越权路径（授权目录之外）'), def: 'confirm' },
  { key: 'delete', label: t('删除文件/目录'), def: 'confirm' },
  { key: 'system_write', label: t('写系统目录'), def: 'block' },
  { key: 'network', label: t('网络外联下载'), def: 'confirm' },
  { key: 'privilege', label: t('提权（sudo 等）'), def: 'block' },
];

function errorReason(err: unknown): string {
  if (err instanceof Error) return err.message;
  const msg = (err as { message?: unknown } | null)?.message;
  return typeof msg === 'string' && msg ? msg : String(err);
}

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
    if (!open) return;
    const timer = window.setTimeout(refresh, 0);
    return () => window.clearTimeout(timer);
  }, [open, refresh]);

  useEffect(() => {
    if (!open) return;
    const onGrant = (e: Event) => {
      const path = (e as CustomEvent<string>).detail;
      if (!path) return;
      addLocalGrant(path)
        .then(refresh)
        .catch((err) => message.error(t('授权失败：{reason}', { reason: errorReason(err) })));
    };
    window.addEventListener('hugagent:grant-folder', onGrant as EventListener);
    return () => window.removeEventListener('hugagent:grant-folder', onGrant as EventListener);
  }, [open, refresh]);

  const changePolicy = (key: keyof LocalPolicy, value: LocalDisposition) => {
    const next = { ...policy, [key]: value };
    setPolicy(next);
    setLocalPolicy(next).catch((err) =>
      message.error(t('保存策略失败：{reason}', { reason: errorReason(err) })),
    );
  };

  return (
    <Modal open={open} onCancel={onClose} title={t('本地权限')} footer={null} width={520} centered>
      <div className="jx-localPermSection">
        <div className="jx-localPermSectionTitle">{t('已授权目录')}</div>
        <div className="jx-localPermHint">{t('工作区权限随当前权限档；额外目录可分别授予只读或读写权限。')}</div>
        {grants.length === 0 && !loading && <div className="jx-localPermEmpty">{t('暂无授权目录')}</div>}
        {grants.map((g) => (
          <div key={g.path} className="jx-localPermRow">
            <span className="jx-localPermPath" title={g.path}>
              {g.path}
            </span>
            <Select
              size="small"
              aria-label={t('修改 {path} 的授权模式', { path: g.path })}
              value={g.mode}
              options={GRANT_MODES}
              popupMatchSelectWidth={false}
              onChange={(mode) =>
                addLocalGrant(g.path, mode)
                  .then(refresh)
                  .catch((err) => message.error(t('保存授权失败：{reason}', { reason: errorReason(err) })))
              }
            />
            <Button
              type="text"
              size="small"
              danger
              onClick={() =>
                removeLocalGrant(g.path)
                  .then(refresh)
                  .catch((err) => message.error(t('撤销授权失败：{reason}', { reason: errorReason(err) })))
              }
            >
              {t('撤销')}
            </Button>
          </div>
        ))}
        <Button
          size="small"
          icon={<PlusOutlined />}
          onClick={() => {
            window.location.href = '/__desktop/pick-grant-folder';
          }}
        >
          {t('授权一个目录')}
        </Button>
      </div>

      <div className="jx-localPermSection">
        <div className="jx-localPermSectionTitle">{t('可回滚的改动')}</div>
        <div className="jx-localPermHint">{t('agent 改动本地文件前会自动快照，可回滚到上一版。')}</div>
        {snaps.length === 0 && <div className="jx-localPermEmpty">{t('暂无可回滚的改动')}</div>}
        {snaps.map((s) => (
          <div key={s.path} className="jx-localPermRow">
            <span className="jx-localPermPath" title={s.path}>
              {s.path}
            </span>
            <span className="jx-localPermMode">{t('{count} 版', { count: s.count })}</span>
            <Button
              type="text"
              size="small"
              onClick={() =>
                rollbackLocalFile(s.path)
                  .then(refresh)
                  .catch((err) => message.error(t('回滚失败：{reason}', { reason: errorReason(err) })))
              }
            >
              {t('回滚')}
            </Button>
          </div>
        ))}
      </div>

      <div className="jx-localPermSection">
        <div className="jx-localPermSectionTitle">{t('危险命令策略')}</div>
        {CATEGORIES.map((c) => (
          <div key={c.key} className="jx-localPermPolicyRow">
            <span className="jx-localPermPolicyLabel">{c.label}</span>
            <Select
              size="small"
              aria-label={c.label}
              value={(policy[c.key] as LocalDisposition) || c.def}
              options={DISPOSITIONS}
              popupMatchSelectWidth={false}
              onChange={(value) => changePolicy(c.key, value)}
            />
          </div>
        ))}
      </div>
    </Modal>
  );
}
