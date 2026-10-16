import { useEffect, useState } from 'react';
import { Empty, Modal, Skeleton, Tabs } from 'antd';
import { motion } from 'motion/react';
import { useSettingsStore } from '../../stores';
import { EASE } from '../../utils/motionTokens';
import { mdToHtml } from '../../utils/markdown';
import type { MemoryItem, MemoryProfile } from '../../types';
import { FactsList } from './FactsList';
import { MemoryGraphView } from './MemoryGraphView';
import { t } from '../../i18n';

interface MemoryManagerModalProps {
  open: boolean;
  onClose: () => void;
}

/**
 * The user's layered memory (profile L1 / facts L2 / graph L3), viewable and editable.
 * Shared by the settings center and the Personal Agent settings; data lives in the settings store.
 */
export function MemoryManagerModal({ open, onClose }: MemoryManagerModalProps) {
  const {
    memoryItems, memoryLoading, memoryProfile, memoryGraph, memoryGraphEnabled,
    loadMemoryAllLayers, loadMemories, removeMemory, clearMemories,
  } = useSettingsStore();
  const [memoryTab, setMemoryTab] = useState('profile');
  // Opening always shows fresh data, whoever opened it.
  useEffect(() => { if (open) void loadMemoryAllLayers(); }, [open, loadMemoryAllLayers]);

  return (
    <Modal
      title={t('我的分层记忆')}
      open={open}
      onCancel={onClose}
      footer={null}
      // 图谱页要摆得下画布 + 关系抽屉，比另外两页宽一档
      width={memoryTab === 'graph' ? 960 : 720}
    >
      {memoryLoading ? (
        <div aria-busy="true"><Skeleton active title={false} paragraph={{ rows: 4 }} style={{ padding: '16px 0' }} /></div>
      ) : (
        <Tabs
          activeKey={memoryTab}
          onChange={setMemoryTab}
          animated={{ tabPane: false }}
          destroyOnHidden
          items={[
            {
              key: 'profile',
              label: tabLabel(t('档案 L1'), memoryProfile?.length, t('字')),
              pane: renderProfileTab(memoryProfile),
            },
            {
              key: 'facts',
              label: tabLabel(t('做法 L2'), memoryItems.length),
              pane: renderFactsTab(memoryItems, removeMemory, clearMemories, loadMemories),
            },
            {
              key: 'graph',
              label: tabLabel(t('图谱 L3'), memoryGraphEnabled ? memoryGraph.length : undefined),
              pane: <MemoryGraphView relations={memoryGraph} enabled={memoryGraphEnabled} onReload={loadMemories} />,
            },
          ].map(({ key, label, pane }) => ({
            key,
            label,
            // destroyOnHidden remounts on Tab switch → uniformly play the enter animation once
            children: (
              <motion.div
                key={`memory-tab-${key}`}
                initial={{ opacity: 0, x: 8 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ duration: 0.18, ease: EASE.standard }}
              >
                {pane}
              </motion.div>
            ),
          }))}
        />
      )}
    </Modal>
  );
}

function tabLabel(base: string, count: number | undefined, unit: string = ''): string {
  if (count === undefined || count === null || count === 0) return base;
  return `${base} · ${count}${unit}`;
}

function renderProfileTab(profile: MemoryProfile | null) {
  if (!profile || !profile.enabled) {
    return <Empty className="jx-anim-fadeIn" description={t('永久记忆未启用')} />;
  }
  const ratio = profile.max_chars > 0 ? Math.min(100, Math.round((profile.length / profile.max_chars) * 100)) : 0;
  if (!profile.content_md) {
    return (
      <div>
        <div style={{ fontSize: 12, color: 'var(--color-text-tertiary)', marginBottom: 8 }}>
          {t('会话启动时冻结注入的用户画像 markdown（上限 {n} 字符）', { n: profile.max_chars })}
        </div>
        <Empty className="jx-anim-fadeIn" description={t('档案为空，开始对话后智能体会自动沉淀你的身份和偏好')} />
      </div>
    );
  }
  return (
    <div>
      <div style={{ fontSize: 12, color: 'var(--color-text-tertiary)', marginBottom: 8 }}>
        {t('每次会话启动时冻结注入 · {used}/{max} 字 ({pct}%)', { used: profile.length, max: profile.max_chars, pct: ratio })}
      </div>
      <div className="jx-settings-profileMeter" role="progressbar" aria-valuenow={ratio} aria-valuemin={0} aria-valuemax={100}>
        <motion.div
          className={`jx-settings-profileMeterFill${ratio > 85 ? ' warn' : ''}`}
          initial={{ width: 0 }}
          animate={{ width: `${ratio}%` }}
          transition={{ duration: 0.5, ease: 'easeOut' }}
        />
      </div>
      <div
        style={{
          padding: 12, background: 'var(--color-bg-gray)', borderRadius: 6,
          maxHeight: 400, overflow: 'auto', fontSize: 13, lineHeight: 1.7,
        }}
        dangerouslySetInnerHTML={{ __html: mdToHtml(profile.content_md) }}
      />
    </div>
  );
}

function renderFactsTab(
  items: MemoryItem[],
  removeMemory: (id: string) => Promise<void>,
  clearMemories: () => Promise<void>,
  reload: () => void,
) {
  return (
    <FactsList
      items={items}
      onRemove={removeMemory}
      onClearAll={clearMemories}
      onEdited={reload}
    />
  );
}
