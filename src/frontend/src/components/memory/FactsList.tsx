import { useState, type CSSProperties } from 'react';
import { Button, Empty, Tag, Typography } from 'antd';
import { DeleteOutlined } from '@ant-design/icons';
import { AnimatePresence, motion } from 'motion/react';
import type { MemoryItem } from '../../types';
import { formatDateTime } from '../../utils/date';
import { EASE, LAYOUT_ANIM_MAX_ITEMS } from '../../utils/motionTokens';
import { t } from '../../i18n';

interface FactsListProps {
  items: MemoryItem[];
  onRemove: (id: string) => Promise<void>;
  onClearAll?: () => Promise<void>;
  hint?: string;
  emptyText?: string;
}

// Row exit: when clearing, each row staggers by index; a single delete has no delay (clearing / index passed in via custom).
const ROW_VARIANTS = {
  exit: ({ i, clearing }: { i: number; clearing: boolean }) => ({
    opacity: 0,
    x: -16,
    height: 0,
    marginBottom: 0,
    paddingTop: 0,
    paddingBottom: 0,
    transition: {
      duration: 0.2,
      ease: EASE.exit,
      delay: clearing ? Math.min(i, 10) * 0.02 : 0,
    },
  }),
};

const HEADER_STYLE: CSSProperties = {
  display: 'flex', justifyContent: 'space-between', alignItems: 'center',
  marginBottom: 8, fontSize: 12, color: '#888',
};
const LIST_STYLE: CSSProperties = { listStyle: 'none', margin: 0, padding: 0 };
const ROW_STYLE: CSSProperties = {
  display: 'flex',
  alignItems: 'flex-start',
  gap: 8,
  padding: '10px 0',
  borderBottom: '1px solid rgba(5,5,5,0.06)',
  overflow: 'hidden',
};
const ROW_BODY_STYLE: CSSProperties = { flex: 1, minWidth: 0 };
const ROW_TAGS_STYLE: CSSProperties = { marginTop: 4, display: 'flex', gap: 4, flexWrap: 'wrap' };
const ROW_TIME_STYLE: CSSProperties = { fontSize: 11, color: '#B3B3B3' };

export function FactsList({
  items,
  onRemove,
  onClearAll,
  hint = t('mem0 / Milvus 向量事实，按需检索注入'),
  emptyText = t('暂无事实记忆'),
}: FactsListProps) {
  // Clearing in progress: this state enters the exit variants via custom on the render where the row is still present
  const [clearing, setClearing] = useState(false);

  const handleClearAll = async () => {
    if (!onClearAll) return;
    setClearing(true);
    try {
      await onClearAll();
    } finally {
      setClearing(false);
    }
  };

  return (
    <div>
      <div style={HEADER_STYLE}>
        <span>{hint}</span>
        {onClearAll && (
          <Button size="small" danger onClick={() => void handleClearAll()} disabled={items.length === 0}>
            {t('清空所有事实')}
          </Button>
        )}
      </div>
      <ul style={LIST_STYLE}>
        <AnimatePresence initial={false}>
          {items.map((item: MemoryItem, i) => (
            <motion.li
              key={item.id}
              layout={items.length <= LAYOUT_ANIM_MAX_ITEMS ? 'position' : false}
              custom={{ i, clearing }}
              exit="exit"
              variants={ROW_VARIANTS}
              style={ROW_STYLE}
            >
              <div style={ROW_BODY_STYLE}>
                <Typography.Text style={{ fontSize: 13 }}>{item.memory}</Typography.Text>
                <div style={ROW_TAGS_STYLE}>
                  {item.confidentiality && (
                    <Tag
                      color={item.confidentiality === 'sensitive' ? 'orange' :
                             item.confidentiality === 'internal' ? 'blue' : 'default'}
                    >
                      {item.confidentiality}
                    </Tag>
                  )}
                  {item.source && <Tag>{item.source}</Tag>}
                  {(item.tags || []).map((tag) => <Tag key={tag} color="geekblue">{tag}</Tag>)}
                  {item.updated_at && (
                    <span style={ROW_TIME_STYLE}>
                      {formatDateTime(item.updated_at)}
                    </span>
                  )}
                </div>
              </div>
              <Button
                type="text" danger size="small"
                icon={<DeleteOutlined />}
                onClick={() => void onRemove(item.id)}
              />
            </motion.li>
          ))}
          {items.length === 0 && (
            <li key="empty" className="jx-anim-fadeIn">
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={emptyText}
                style={{ padding: '16px 0' }}
              />
            </li>
          )}
        </AnimatePresence>
      </ul>
    </div>
  );
}
