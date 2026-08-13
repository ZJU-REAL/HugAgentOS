import {
  CloseOutlined,
  FullscreenExitOutlined,
  FullscreenOutlined,
  InsertRowRightOutlined,
} from '@ant-design/icons';
import { useEffect, type ReactNode } from 'react';

import { t } from '../../i18n';
import { useCanvasStore } from '../../stores';

interface CanvasTabBarProps {
  title: string;
  icon: ReactNode;
  closeLabel: string;
  onClose: () => void;
}

export function CanvasTabBar({ title, icon, closeLabel, onClose }: CanvasTabBarProps) {
  const isFullscreen = useCanvasStore((state) => state.isFullscreen);
  const setCanvasFullscreen = useCanvasStore((state) => state.setCanvasFullscreen);
  const toggleCanvasFullscreen = useCanvasStore((state) => state.toggleCanvasFullscreen);

  useEffect(() => {
    if (!isFullscreen) return undefined;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setCanvasFullscreen(false);
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isFullscreen, setCanvasFullscreen]);

  return (
    <div className="jx-canvasTabs" role="tablist" aria-label={t('右侧面板')}>
      <div className="jx-canvasTab" role="tab" aria-selected="true" tabIndex={0} title={title}>
        <span className="jx-canvasTab-icon" aria-hidden="true">{icon}</span>
        <span className="jx-canvasTab-title">{title}</span>
        <button
          type="button"
          className="jx-canvasTab-close"
          onClick={onClose}
          aria-label={closeLabel}
          title={closeLabel}
        >
          <CloseOutlined />
        </button>
      </div>
      <div className="jx-canvasTabs-spacer" aria-hidden="true" />
      <div className="jx-canvasTabs-actions">
        <button
          type="button"
          className="jx-canvasTabs-action jx-canvasTabs-fullscreen"
          onClick={toggleCanvasFullscreen}
          aria-label={isFullscreen ? t('退出全屏') : t('全屏')}
          title={isFullscreen ? t('退出全屏') : t('全屏')}
          aria-pressed={isFullscreen}
        >
          {isFullscreen ? <FullscreenExitOutlined /> : <FullscreenOutlined />}
        </button>
        <button
          type="button"
          className="jx-canvasTabs-action"
          onClick={onClose}
          aria-label={closeLabel}
          title={closeLabel}
        >
          <InsertRowRightOutlined />
        </button>
      </div>
    </div>
  );
}
