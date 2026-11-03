import { t } from '../../i18n';
import { useCanvasStore, type CanvasArtifact } from '../../stores/canvasStore';
import { DeliveryFileIcon } from '../chat/DeliveryFileIcon';
// DeliveryFileIcon takes its per-type colours from the delivery card styles.
import '../chat/DeliveredFiles.css';
import './RunMonitorRail.css';

/** One line per file: icon, name, type. Clicking opens it in the canvas. */
export function MonitorFileRow({ file, active }: { file: CanvasArtifact; active: boolean }) {
  const openCanvas = useCanvasStore((s) => s.openCanvas);
  const extension = file.name.includes('.') ? file.name.split('.').pop()?.toUpperCase() : '';
  return (
    <button
      type="button"
      className="jx-runMonitor-row"
      data-active={active || undefined}
      title={file.name}
      onClick={() => openCanvas(file)}
      aria-label={t('预览文件：{name}', { name: file.name })}
    >
      <span className="jx-runMonitor-lead"><DeliveryFileIcon name={file.name} size={20} /></span>
      <span className="jx-runMonitor-name">{file.name}</span>
      {extension && <span className="jx-runMonitor-meta">{extension}</span>}
    </button>
  );
}
