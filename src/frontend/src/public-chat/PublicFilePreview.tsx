import { Drawer } from 'antd';
import { useCanvasStore } from '../stores/canvasStore';
import { FilePreviewPane } from '../components/file/FilePreviewPane';

export function PublicFilePreview() {
  const { isOpen, activeView, artifact, closeCanvas } = useCanvasStore();
  return <Drawer open={isOpen && activeView === 'file' && !!artifact} onClose={closeCanvas}
    title={artifact?.name} size="large" destroyOnHidden className="jx-publicFilePreview">
    {artifact && <FilePreviewPane item={{
      id: artifact.file_id, file_id: artifact.file_id, name: artifact.name,
      type: artifact.mime_type?.startsWith('image/') ? 'image' : 'document',
      download_url: artifact.url, mime_type: artifact.mime_type, size: artifact.size,
      chat_id: artifact.chat_id, origin: artifact.origin, created_at: '',
    }} />}
  </Drawer>;
}
