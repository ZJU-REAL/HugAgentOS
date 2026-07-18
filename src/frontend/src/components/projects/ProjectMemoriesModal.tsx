import { useCallback, useEffect, useState } from 'react';
import { Modal, Spin, message } from 'antd';
import type { MemoryItem } from '../../types';
import { getMemories, deleteMemory } from '../../api';
import { FactsList } from '../memory/FactsList';
import { t } from '../../i18n';

interface ProjectMemoriesModalProps {
  open: boolean;
  projectId: string;
  projectName?: string;
  onClose: () => void;
  /** Notify the parent component to refresh the count after deletion */
  onChange?: () => void;
}

export default function ProjectMemoriesModal({
  open, projectId, projectName, onClose, onChange,
}: ProjectMemoriesModalProps) {
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [enabled, setEnabled] = useState(true);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const data = await getMemories(projectId);
      setEnabled(data.enabled);
      setItems(data.items || []);
    } catch (err) {
      message.error((err as Error)?.message || t('加载项目记忆失败'));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    if (open) void reload();
  }, [open, reload]);

  const handleRemove = useCallback(async (id: string) => {
    try {
      await deleteMemory(id);
      setItems((prev) => prev.filter((m) => m.id !== id));
      onChange?.();
    } catch (err) {
      message.error((err as Error)?.message || t('删除失败'));
    }
  }, [onChange]);

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width={720}
      title={projectName ? t('项目记忆 · {name}', { name: projectName }) : t('项目记忆')}
      destroyOnClose
    >
      {!enabled ? (
        <div style={{ padding: 24, textAlign: 'center', color: '#888' }}>
          {t('项目记忆未启用（mem0 关闭或本项目读取已关闭）。')}
        </div>
      ) : loading ? (
        <div style={{ padding: 24, textAlign: 'center' }}>
          <Spin />
        </div>
      ) : (
        <FactsList
          items={items}
          onRemove={handleRemove}
          hint={t('仅本项目可见的事实记忆（按需检索注入到对话）')}
          emptyText={t('该项目暂无事实记忆，几轮对话之后会在这里出现。')}
        />
      )}
    </Modal>
  );
}
