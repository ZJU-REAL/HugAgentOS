import { useCallback } from 'react';
import { Modal } from 'antd';
import { t } from '../../i18n';
import { useCanvasStore } from '../../stores/canvasStore';

export function useCanvasGuard() {
  return useCallback((action: () => void) => {
    const state = useCanvasStore.getState();
    const active = state.tabs.find((tab) => tab.id === state.activeTabId);
    if (!active || active.kind !== 'file' || !active.dirty) {
      action();
      return;
    }
    Modal.confirm({
      title: t('有未保存的修改'),
      content: t('离开后编辑内容将丢失，确定继续？'),
      okText: t('放弃修改'),
      cancelText: t('取消'),
      okButtonProps: { danger: true },
      onOk: action,
    });
  }, []);

}
