import { useState } from 'react';
import { DownOutlined, UpOutlined } from '@ant-design/icons';
import { DeliveryFileIcon } from './DeliveryFileIcon';
import { useCanvasStore } from '../../stores/canvasStore';
import { t } from '../../i18n';
import { artifactOrigin, type ArtifactLocation } from '../../utils/artifactAccess';
import { DeliveredFileActions, type DeliveryPhase } from './DeliveredFileActions';
import './DeliveredFiles.css';

export interface ArtifactRef extends ArtifactLocation {
  file_id: string;
  name?: string;
  url?: string;
  mime_type?: string;
  size?: number;
  description?: string;
}

function DeliveredFile({ file }: { file: ArtifactRef }) {
  const openCanvas = useCanvasStore((state) => state.openCanvas);
  const [phase, setPhase] = useState<DeliveryPhase>();
  const [path, setPath] = useState('');
  const name = file.name?.split(/[\\/]/).pop() || t('文件');
  const extension = name.includes('.') ? name.split('.').pop()?.toUpperCase() : '';
  const description = file.description?.replace(/\s*(?:\([^()]*\)|（[^（）]*）)\s*$/u, '').trim();
  const status = phase?.message || description || extension || t('文件');
  return <div className="hg-delivery-file" data-presented-file>
    <button type="button" className="hg-delivery-preview"
      title={path || file.name} aria-label={t('预览文件：{name}', { name })}
      onClick={() => openCanvas({
        ...file, name, url: file.url || '', origin: artifactOrigin(file),
      })} />
    <span className="hg-delivery-icon">
      <DeliveryFileIcon name={name} />
    </span>
    <div className="hg-delivery-body">
      <div className="hg-delivery-details">
        <span className="hg-delivery-name">{name}</span>
        <span className="hg-delivery-description" role={phase ? 'status' : undefined}
          data-error={phase?.kind === 'error' || undefined}>
          <span className="hg-delivery-secondary" data-success={phase?.kind === 'success' || undefined}>{status}</span>
          <span className="hg-delivery-hint">{t('预览')}</span>
        </span>
      </div>
      <div className="hg-delivery-actions">
        <DeliveredFileActions file={file} onPhase={setPhase} onPath={setPath} />
      </div>
    </div>
  </div>;
}

export function DeliveredFiles({ artifacts, chatId }: { artifacts: ArtifactRef[]; chatId?: string }) {
  const [expanded, setExpanded] = useState(false);
  const files = Array.from(new Map(artifacts.map(file => [file.file_id, file])).values());
  if (!files.length) return null;
  const visible = expanded ? files : files.slice(0, 4);
  return <div className="hg-deliveries">
    <div className="hg-deliveries-grid" data-presented-files-row data-single={files.length === 1 || undefined}>
      {visible.map(source => {
        const file = { ...source, chat_id: source.chat_id || chatId };
        return <DeliveredFile key={file.file_id} file={file} />;
      })}
    </div>
    {files.length > 4 && <button type="button" className="hg-deliveries-toggle" aria-expanded={expanded}
      aria-label={t(expanded ? '收起交付文件' : '显示全部 {count} 个文件', { count: files.length })}
      onClick={() => setExpanded(value => !value)}>
      <span>{t(expanded ? '收起' : '全部 {count} 个文件', { count: files.length })}</span>
      {expanded ? <UpOutlined /> : <DownOutlined />}
    </button>}
  </div>;
}
