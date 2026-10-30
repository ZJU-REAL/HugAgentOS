import { InfoCircleOutlined } from '@ant-design/icons';
import { InputNumber, Switch, Tooltip, message } from 'antd';
import { useEffect, useState } from 'react';

import { getEvolutionPrefs, updateEvolutionPrefs } from '../../api';
import type { EvolutionPrefs } from '../../api';
import { t } from '../../i18n';
import { LoadError } from '../common/LoadState';

const MECHANISMS: Array<{ key: string; label: string; desc: string }> = [
  { key: 'memory', label: '记忆生长', desc: '把事实与偏好沉淀为长期记忆' },
  { key: 'skill', label: '技能沉淀', desc: '把反复出现的做法整理成可复用技能' },
  { key: 'workflow', label: '编排修正', desc: '调整步骤顺序与流程策略' },
];

/**
 * Fine-grained evolution configuration.
 *
 * Mechanisms, distillation threshold and related switches beyond the single
 * on/off control.
 */
export function EvolutionPrefsPanel({ onShowDetail }: { onShowDetail?: () => void }) {
  const [prefs, setPrefs] = useState<EvolutionPrefs | null>(null);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let disposed = false;
    getEvolutionPrefs()
      .then((data) => {
        if (disposed) return;
        setPrefs(data.prefs);
      })
      .catch((err) => { if (!disposed) setLoadError((err as Error).message || t('加载失败')); });
    return () => { disposed = true; };
  }, [attempt]);

  const retry = () => { setLoadError(''); setAttempt((n) => n + 1); };

  if (!prefs) {
    return loadError
      ? <LoadError title={t('进化设置加载失败')} reason={loadError} onRetry={retry} />
      : (
        <div className="jx-evoPrefs" role="status" aria-label={t('加载中…')} aria-busy="true">
          {Array.from({ length: 5 }, (_, index) => (
            <div className="jx-evoPrefs-row" key={index} aria-hidden="true">
              <div className="jx-evoPrefs-label">
                <div className="jx-skeletonBlock jx-evoPrefs-skLabel" />
                <div className="jx-skeletonBlock jx-evoPrefs-skDesc" />
              </div>
              <div className="jx-skeletonBlock jx-evoPrefs-skControl" />
            </div>
          ))}
        </div>
      );
  }

  const patch = (change: Partial<EvolutionPrefs>) => {
    const previous = prefs;
    setPrefs({ ...prefs, ...change });
    setSaving(true);
    updateEvolutionPrefs(change)
      .then((saved) => setPrefs(saved))
      // Revert rather than leave the control showing a value the server
      // rejected — a switch that lies is worse than one that flickers.
      .catch(() => {
        setPrefs(previous);
        message.error(t('配置保存失败'));
      })
      .finally(() => setSaving(false));
  };

  const toggleMechanism = (key: string, on: boolean) => {
    const next = on
      ? [...new Set([...prefs.mechanisms, key])]
      : prefs.mechanisms.filter((m) => m !== key);
    // The backend refuses an empty selection because it would silently disable
    // evolution without saying so; surface that here instead of a bare error.
    if (next.length === 0) {
      message.warning(t('至少保留一项进化机制；如需完全停止，请关闭「启动进化」'));
      return;
    }
    patch({ mechanisms: next });
  };


  return (
    <div className="jx-evoPrefs">
      {/* The single evolution switch: distilling for this user and their
          conversations serving as evidence both follow it — there is no
          separate participation toggle. */}
      <div className="jx-evoPrefs-row">
        <div className="jx-evoPrefs-label">
          <span>{t('启动进化')}</span>
          <small>{t('开启后，系统从你的用法中为你沉淀能力，你的对话也会作为证据参与集体能力沉淀；关闭后全部停止，已生成的内容保留')}</small>
        </div>
        <Switch
          checked={prefs.enabled}
          disabled={saving}
          onChange={(on) => patch({ enabled: on })}
        />
      </div>

      {prefs.enabled && onShowDetail && (
        <div className="jx-settings-memoryDetail">
          <span className="jx-settings-memoryCount">
            {t('你的对话正在参与能力沉淀')}
          </span>
          <a className="jx-settings-memoryLink" onClick={onShowDetail}>
            {t('查看进化详情')}
          </a>
        </div>
      )}

      <div className="jx-evoPrefs-row">
        <div className="jx-evoPrefs-label">
          <span>{t('进化机制')}</span>
          <small>{t('选择允许系统为你沉淀哪些类型的能力')}</small>
        </div>
        <div className="jx-evoPrefs-mechs">
          {MECHANISMS.map((mech) => (
            <label key={mech.key} className="jx-evoPrefs-mech">
              <Switch
                size="small"
                checked={prefs.mechanisms.includes(mech.key)}
                disabled={!prefs.enabled}
                onChange={(on) => toggleMechanism(mech.key, on)}
              />
              <span className="jx-evoPrefs-mechName">{t(mech.label)}</span>
              <small>{t(mech.desc)}</small>
            </label>
          ))}
        </div>
      </div>

      <div className="jx-evoPrefs-row">
        <div className="jx-evoPrefs-label">
          <span>{t('沉淀门槛')}</span>
          <small>{t('同一做法至少重复多少次才会被整理成能力候选')}</small>
        </div>
        <InputNumber
          size="small"
          min={2}
          max={20}
          value={prefs.min_support}
          disabled={saving || !prefs.enabled}
          onChange={(value) => value && patch({ min_support: value })}
        />
      </div>

      <div className="jx-evoPrefs-row">
        <div className="jx-evoPrefs-label">
          <span>
            {t('低风险候选自动启用')}
            <Tooltip
              title={t('仅对完全来自你自己对话、且影响范围仅限你个人的候选生效')}
            >
              <InfoCircleOutlined className="jx-evoPrefs-hint" />
            </Tooltip>
          </span>
          <small>{t('关闭时，每个候选都需要你手动确认')}</small>
        </div>
        <Switch
          checked={prefs.auto_approve_low_risk}
          disabled={saving || !prefs.enabled}
          onChange={(on) => patch({ auto_approve_low_risk: on })}
        />
      </div>
    </div>
  );
}
