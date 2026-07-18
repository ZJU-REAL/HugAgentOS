import { useCallback, useRef, useState } from 'react';

const hasFilePayload = (e: React.DragEvent) =>
  Array.from(e.dataTransfer?.types ?? []).includes('Files');

/** Drag-and-drop upload zone: enter/leave counter debouncing (crossing child elements fires successive dragenter/leave events).
 *  Usage: const { dragActive, dropZoneProps } = useFileDropZone(enabled, onDropFiles);
 *  Spread dropZoneProps onto the drop-zone container; dragActive drives <DropOverlay />. */
export function useFileDropZone(
  enabled: boolean,
  onDropFiles: (files: FileList) => void,
) {
  const dragDepthRef = useRef(0);
  const [dragActive, setDragActive] = useState(false);

  const onDragEnter = useCallback((e: React.DragEvent) => {
    if (!enabled || !hasFilePayload(e)) return;
    e.preventDefault();
    dragDepthRef.current += 1;
    setDragActive(true);
  }, [enabled]);

  const onDragOver = useCallback((e: React.DragEvent) => {
    if (!enabled || !hasFilePayload(e)) return;
    e.preventDefault();
  }, [enabled]);

  const onDragLeave = useCallback((e: React.DragEvent) => {
    if (!enabled || !hasFilePayload(e)) return;
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) setDragActive(false);
  }, [enabled]);

  const onDrop = useCallback((e: React.DragEvent) => {
    if (!enabled) return;
    e.preventDefault();
    dragDepthRef.current = 0;
    setDragActive(false);
    const files = e.dataTransfer?.files;
    if (!files || files.length === 0) return;
    onDropFiles(files);
  }, [enabled, onDropFiles]);

  return {
    dragActive,
    dropZoneProps: { onDragEnter, onDragOver, onDragLeave, onDrop },
  };
}
