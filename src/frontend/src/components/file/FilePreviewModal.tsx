import { Modal } from 'antd';
import type { ResourceItem } from '../../types';
import { t } from '../../i18n';
import { FilePreviewPane } from './FilePreviewPane';
import './FilePreviewModal.css';

/** A definite, viewport-bounded height keeps iframe and table previews usable. */
export function FilePreviewModal({ item, title, onClose }: {
  item: ResourceItem | null;
  title?: string;
  onClose: () => void;
}) {
  return (
    <Modal
      open={!!item}
      title={title || item?.name || t('文件预览')}
      onCancel={onClose}
      footer={null}
      width="min(1100px, calc(100vw - 32px))"
      className="jx-filePreviewModal"
      style={{ top: 16, paddingBottom: 0 }}
      destroyOnHidden
    >
      <FilePreviewPane item={item} />
    </Modal>
  );
}
