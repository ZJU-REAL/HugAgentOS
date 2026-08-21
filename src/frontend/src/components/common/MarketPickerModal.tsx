import { useEffect, useMemo, useState } from 'react';
import { Button, Empty, Input, Modal, Tag } from 'antd';
import { CheckOutlined, PlusOutlined, SearchOutlined } from '@ant-design/icons';
import { t } from '../../i18n';

export interface MarketPickItem {
  id: string;
  name?: string;
  description?: string;
}

/**
 * 市场能力选择弹窗（技能 / 智能体共用）。
 *
 * 为什么不直接塞进下拉：市场是开放清单，几百上千项灌进 Select 分组里既滚不动也
 * 搜不清。这里给一个带搜索的独立面板，逐项看名字和简介再点选，确认后一次性加回
 * 表单；真正的安装发生在保存模式时（后端按引用自动装）。
 */
export function MarketPickerModal({
  open,
  title,
  items,
  selected,
  onClose,
  onAdd,
}: {
  open: boolean;
  title: string;
  items: MarketPickItem[];
  /** 表单里已经选中的 id（含已装项与之前挑的市场项），用于显示"已添加" */
  selected: string[];
  onClose: () => void;
  onAdd: (picked: MarketPickItem[]) => void;
}) {
  const [q, setQ] = useState('');
  const [picked, setPicked] = useState<Map<string, MarketPickItem>>(new Map());

  // 每次打开都从空选起：上一轮挑完的已经进表单了，残留勾选只会造成重复
  useEffect(() => {
    if (open) {
      setQ('');
      setPicked(new Map());
    }
  }, [open]);

  const selectedSet = useMemo(() => new Set(selected), [selected]);

  const filtered = useMemo(() => {
    const kw = q.trim().toLowerCase();
    if (!kw) return items;
    return items.filter((i) =>
      (i.name || '').toLowerCase().includes(kw)
      || (i.description || '').toLowerCase().includes(kw)
      || i.id.toLowerCase().includes(kw));
  }, [items, q]);

  const toggle = (item: MarketPickItem) => {
    setPicked((prev) => {
      const next = new Map(prev);
      if (next.has(item.id)) next.delete(item.id);
      else next.set(item.id, item);
      return next;
    });
  };

  return (
    <Modal
      open={open}
      title={title}
      onCancel={onClose}
      onOk={() => { onAdd(Array.from(picked.values())); onClose(); }}
      okText={picked.size > 0 ? t('添加 {n} 项', { n: String(picked.size) }) : t('添加')}
      okButtonProps={{ disabled: picked.size === 0 }}
      width={640}
      destroyOnClose
    >
      <Input
        allowClear
        prefix={<SearchOutlined style={{ color: 'var(--color-text-placeholder)' }} />}
        placeholder={t('搜索名称、简介或标识…')}
        value={q}
        onChange={(e) => setQ(e.target.value)}
        style={{ marginBottom: 12 }}
      />
      <div style={{ maxHeight: '52vh', overflowY: 'auto' }}>
        {filtered.length === 0 ? (
          <Empty description={t('没有匹配的市场项')} image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : filtered.map((item) => {
          const already = selectedSet.has(item.id);
          const isPicked = picked.has(item.id);
          return (
            <div
              key={item.id}
              onClick={() => { if (!already) toggle(item); }}
              style={{
                display: 'flex',
                alignItems: 'flex-start',
                gap: 12,
                padding: '10px 12px',
                borderRadius: 10,
                cursor: already ? 'default' : 'pointer',
                background: isPicked ? 'rgba(18,109,255,.06)' : 'transparent',
                opacity: already ? 0.55 : 1,
              }}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 14, fontWeight: 500, color: 'var(--color-text)' }}>
                  {item.name || item.id}
                  {already && <Tag style={{ marginLeft: 8 }}>{t('已添加')}</Tag>}
                </div>
                <div style={{
                  fontSize: 12, color: 'var(--color-text-tertiary)',
                  overflow: 'hidden', textOverflow: 'ellipsis',
                  display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical',
                }}>
                  {item.description || t('（没有简介）')}
                </div>
              </div>
              {!already && (
                <Button
                  size="small"
                  type={isPicked ? 'primary' : 'default'}
                  icon={isPicked ? <CheckOutlined /> : <PlusOutlined />}
                  onClick={(e) => { e.stopPropagation(); toggle(item); }}
                >
                  {isPicked ? t('已选') : t('选择')}
                </Button>
              )}
            </div>
          );
        })}
      </div>
      <div style={{ marginTop: 10, fontSize: 12, color: 'var(--color-text-tertiary)' }}>
        {t('选中的项会在保存模式时自动安装。')}
      </div>
    </Modal>
  );
}

export default MarketPickerModal;
