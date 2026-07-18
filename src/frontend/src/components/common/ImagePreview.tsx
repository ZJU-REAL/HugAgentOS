import { useEffect, useRef, useState } from 'react';
import type { PointerEvent as ReactPointerEvent, WheelEvent as ReactWheelEvent } from 'react';
import { Modal } from 'antd';
import { t } from '../../i18n';
import {
  CloseOutlined,
  ReloadOutlined,
  ZoomInOutlined,
  ZoomOutOutlined,
} from '@ant-design/icons';
import { useUIStore } from '../../stores';

const MIN_SCALE = 0.2;
const MAX_SCALE = 8;
const STEP = 1.2; // multiplicative zoom factor per wheel notch / button press

function clampScale(s: number): number {
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, s));
}

function isSvgSource(name: string, url: string): boolean {
  const probe = `${name || ''} ${url || ''}`.toLowerCase();
  return /\.svg(\?|#|$)/.test(probe) || probe.includes('image/svg');
}

export default function ImagePreview() {
  const previewImage = useUIStore((s) => s.previewImage);
  const setPreviewImage = useUIStore((s) => s.setPreviewImage);

  const [scale, setScale] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  // Mirrors dragRef as state so the img can drop its CSS transition while
  // panning (frame-accurate follow) and restore it afterwards.
  const [dragging, setDragging] = useState(false);
  const dragRef = useRef<{ px: number; py: number; ox: number; oy: number } | null>(null);

  // Reset the transform whenever a different image is opened.
  useEffect(() => {
    setScale(1);
    setOffset({ x: 0, y: 0 });
  }, [previewImage?.url]);

  const close = () => setPreviewImage(null);
  const reset = () => {
    setScale(1);
    setOffset({ x: 0, y: 0 });
  };
  const zoomBy = (factor: number) =>
    setScale((s) => {
      const next = clampScale(s * factor);
      if (next === 1) setOffset({ x: 0, y: 0 });
      return next;
    });

  const onWheel = (e: ReactWheelEvent) => {
    e.preventDefault();
    zoomBy(e.deltaY < 0 ? STEP : 1 / STEP);
  };

  const onPointerDown = (e: ReactPointerEvent) => {
    if (scale <= 1) return; // only pan when zoomed in
    e.preventDefault();
    (e.target as Element).setPointerCapture?.(e.pointerId);
    dragRef.current = { px: e.clientX, py: e.clientY, ox: offset.x, oy: offset.y };
    setDragging(true);
  };
  const onPointerMove = (e: ReactPointerEvent) => {
    if (!dragRef.current) return;
    setOffset({
      x: dragRef.current.ox + (e.clientX - dragRef.current.px),
      y: dragRef.current.oy + (e.clientY - dragRef.current.py),
    });
  };
  const endDrag = () => {
    dragRef.current = null;
    setDragging(false);
  };

  const isSvg = previewImage ? isSvgSource(previewImage.name, previewImage.url) : false;

  return (
    <Modal
      title={null}
      open={!!previewImage}
      onCancel={close}
      footer={null}
      width="auto"
      centered
      closable={false}
      maskClosable
      className="jx-imagePreviewModal"
      rootClassName="jx-imagePreviewRoot"
      destroyOnClose
    >
      {previewImage && (
        <div className="jx-imagePreviewStage" onWheel={onWheel}>
          <div
            className="jx-imagePreviewCanvas"
            style={{ cursor: scale > 1 ? (dragRef.current ? 'grabbing' : 'grab') : 'default' }}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={endDrag}
            onPointerCancel={endDrag}
            // Click on empty backdrop closes; clicks on the image (when not panning) do nothing.
            onClick={(e) => {
              if (e.target === e.currentTarget) close();
            }}
          >
            <img
              src={previewImage.url}
              alt={previewImage.name}
              draggable={false}
              className={`jx-imagePreviewImg${isSvg ? ' jx-imagePreviewImg--svg' : ''}${dragging ? ' dragging' : ''}`}
              style={{
                transform: `translate(${offset.x}px, ${offset.y}px) scale(${scale})`,
              }}
            />
          </div>

          <div className="jx-imagePreviewToolbar" onClick={(e) => e.stopPropagation()}>
            <button
              type="button"
              className="jx-imagePreviewTool"
              title={t('缩小')}
              onClick={() => zoomBy(1 / STEP)}
            >
              <ZoomOutOutlined />
            </button>
            <span className="jx-imagePreviewZoomLabel">{Math.round(scale * 100)}%</span>
            <button
              type="button"
              className="jx-imagePreviewTool"
              title={t('放大')}
              onClick={() => zoomBy(STEP)}
            >
              <ZoomInOutlined />
            </button>
            <button
              type="button"
              className="jx-imagePreviewTool"
              title={t('重置')}
              onClick={reset}
            >
              <ReloadOutlined />
            </button>
            <button
              type="button"
              className="jx-imagePreviewTool"
              title={t('关闭')}
              onClick={close}
            >
              <CloseOutlined />
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}
