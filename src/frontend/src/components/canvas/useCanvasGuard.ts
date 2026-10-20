import { useCallback } from 'react';
import { Modal } from 'antd';
import { t } from '../../i18n';
import { useCanvasPaneId } from './useCanvasPane';
import { useCanvasStore } from '../../stores/canvasStore';

export function useCanvasGuard() {
  const pane = useCanvasPaneId();
  return useCallback((action: () => void, scope: 'pane' | 'all' = 'pane') => {
    const state = useCanvasStore.getState();
    const ids = scope === 'all' ? Object.values(state.paneTabs) : [state.paneTabs[pane]];
    if (!state.tabs.some(tab => ids.includes(tab.id) && tab.kind === 'file' && tab.dirty)) {
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
  }, [pane]);

}
