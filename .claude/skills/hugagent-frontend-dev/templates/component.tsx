/**
 * Component template.
 *
 * Replace ${ComponentName}, ${componentName}, ${group} with actual names.
 * Create as components/${group}/${ComponentName}.tsx
 */

import React, { useState, useMemo, useCallback, useRef } from 'react';
import { Button, Space, message } from 'antd';
// import { useChatStore } from '../../stores';
// import type { SomeType } from '../../types';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface ${ComponentName}Props {
  /** 必填属性示例 */
  itemId: string;
  /** 回调示例 */
  onClose: () => void;
  /** 可选属性示例 */
  items?: any[];
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ${ComponentName}({ itemId, onClose, items = [] }: ${ComponentName}Props) {
  // --- Zustand stores (global state) ---
  // const { someState, someAction } = useSomeStore();

  // --- Local state ---
  const [loading, setLoading] = useState(false);
  const [editMode, setEditMode] = useState(false);

  // --- Refs ---
  const inputRef = useRef<HTMLInputElement>(null);

  // --- Derived state (useMemo) ---
  const filteredItems = useMemo(() => {
    return items.filter((item) => item.active);
  }, [items]);

  // --- Callbacks (useCallback) ---
  const handleSubmit = useCallback(async () => {
    setLoading(true);
    try {
      // await someApiCall();
      message.success('操作成功');
      onClose();
    } catch (e) {
      message.error(`操作失败：${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, [itemId, onClose]);

  // --- Early return for empty state ---
  if (!filteredItems.length) {
    return (
      <div className="jx-${componentName}" style={{ textAlign: 'center', padding: 40 }}>
        <p style={{ color: 'var(--muted)' }}>暂无数据</p>
      </div>
    );
  }

  // --- Render ---
  return (
    <div className="jx-${componentName}">
      <div className="jx-${componentName}-header">
        <h3>{filteredItems.length} 项</h3>
        <Space>
          <Button onClick={() => setEditMode(!editMode)}>
            {editMode ? '取消' : '编辑'}
          </Button>
          <Button type="primary" onClick={handleSubmit} loading={loading}>
            提交
          </Button>
        </Space>
      </div>

      <div className="jx-${componentName}-list">
        {filteredItems.map((item) => (
          <div key={item.id} className="jx-${componentName}-item">
            {item.name}
          </div>
        ))}
      </div>
    </div>
  );
}
