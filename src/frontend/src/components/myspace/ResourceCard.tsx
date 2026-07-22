import {
  CopyOutlined,
  DeleteOutlined,
  DownloadOutlined,
  ExportOutlined,
  FolderOpenOutlined,
  InboxOutlined,
  MoreOutlined,
} from '@ant-design/icons';
import { Checkbox, Dropdown, Popover } from 'antd';
import type { MenuProps } from 'antd';

import { t } from '../../i18n';
import type { ResourceItem } from '../../types';
import { formatFileSize } from '../../utils/codeExecUtils';
import { confirmDelete } from '../../utils/confirmDelete';
import { formatDateTime } from '../../utils/date';
import { getFileIconSrc } from '../../utils/fileIcon';

interface ResourceCardProps {
  item: ResourceItem;
  checked: boolean;
  anySelected: boolean;
  onCheck: (checked: boolean) => void;
  onDownload?: (item: ResourceItem) => void;
  onNavigate?: (item: ResourceItem) => void;
  onDelete?: (item: ResourceItem) => void;
  onPreview?: (item: ResourceItem) => void;
  onAddToKb?: (item: ResourceItem) => void;
  onMoveToPersonalFolder?: (item: ResourceItem) => void;
  onCopyToPersonalFolder?: (item: ResourceItem) => void;
}

export function ResourceCard({
  item,
  checked,
  anySelected,
  onCheck,
  onDownload,
  onNavigate,
  onDelete,
  onPreview,
  onAddToKb,
  onMoveToPersonalFolder,
  onCopyToPersonalFolder,
}: ResourceCardProps) {
  const knowledgeBaseCount = item.knowledge_base_count ?? 0;
  const knowledgeBases = item.knowledge_bases ?? [];
  const menuItems: MenuProps['items'] = [];

  if (onAddToKb && item.file_id) {
    menuItems.push({
      key: 'addToKb',
      icon: <InboxOutlined />,
      label: t('加入知识库'),
      onClick: () => onAddToKb(item),
    });
  }
  if (onMoveToPersonalFolder && item.file_id) {
    menuItems.push({
      key: 'moveToPersonalFolder',
      icon: <FolderOpenOutlined />,
      label: t('移动到文件夹'),
      onClick: () => onMoveToPersonalFolder(item),
    });
  }
  if (onCopyToPersonalFolder && item.file_id) {
    menuItems.push({
      key: 'copyToPersonalFolder',
      icon: <CopyOutlined />,
      label: t('复制到文件夹'),
      onClick: () => onCopyToPersonalFolder(item),
    });
  }
  if (onDownload && item.file_id) {
    menuItems.push({
      key: 'download',
      icon: <DownloadOutlined />,
      label: t('下载'),
      onClick: () => onDownload(item),
    });
  }
  if (onNavigate && item.source_chat_id) {
    menuItems.push({
      key: 'navigate',
      icon: <ExportOutlined />,
      label: t('跳转到对话'),
      onClick: () => onNavigate(item),
    });
  }
  if (onDelete) {
    if (menuItems.length > 0) menuItems.push({ type: 'divider' });
    menuItems.push({
      key: 'delete',
      icon: <DeleteOutlined style={{ color: 'var(--color-error)' }} />,
      label: <span style={{ color: 'var(--color-error)' }}>{t('删除')}</span>,
      onClick: () => confirmDelete(item.name, () => onDelete(item)),
    });
  }

  const sizeLabel = typeof item.size === 'number' && item.size > 0 ? formatFileSize(item.size) : '--';
  const sourceLabel = item.source_kind === 'ai_generated'
    ? t('AI生成')
    : item.source_kind === 'user_upload'
      ? t('用户上传')
      : '--';
  const openPreview = onPreview && item.file_id ? () => onPreview(item) : undefined;

  return (
    <div
      className={`jx-mySpace-docRow${checked ? ' jx-mySpace-docRow--checked' : ''}${anySelected ? ' jx-mySpace-docRow--anySelected' : ''}`}
      onClick={openPreview}
      style={{ cursor: openPreview ? 'pointer' : 'default' }}
    >
      <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--check">
        <Checkbox
          checked={checked}
          onChange={(event) => { event.stopPropagation(); onCheck(event.target.checked); }}
          onClick={(event) => event.stopPropagation()}
        />
      </div>
      <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--name">
        <img className="jx-mySpace-docRow-icon" src={getFileIconSrc(item.name)} alt="" aria-hidden="true" />
        <div className="jx-mySpace-docRow-nameWrap">
          <span className="jx-mySpace-docRow-name" title={item.name}>{item.name}</span>
          {knowledgeBaseCount > 0 && (
            <Popover
              trigger="click"
              placement="bottomLeft"
              overlayClassName="jx-mySpace-kbUsageOverlay"
              content={(
                <div className="jx-mySpace-kbUsagePopover">
                  {knowledgeBases.map((kb) => (
                    <div key={kb.kb_id} className="jx-mySpace-kbUsageItem">{kb.name}</div>
                  ))}
                </div>
              )}
            >
              <button type="button" className="jx-mySpace-kbBadge" onClick={(event) => event.stopPropagation()}>
                <InboxOutlined style={{ fontSize: 11 }} />
                <span>{t('{n}个知识库', { n: knowledgeBaseCount })}</span>
              </button>
            </Popover>
          )}
        </div>
      </div>
      <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--size">
        <span className="jx-mySpace-docRow-meta">{sizeLabel}</span>
      </div>
      <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--source">
        <span className="jx-mySpace-docRow-meta">{sourceLabel}</span>
      </div>
      <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--time">
        <span className="jx-mySpace-docRow-time">{formatDateTime(item.created_at, '')}</span>
      </div>
      <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--actions">
        {menuItems.length > 0 && (
          <Dropdown
            menu={{ items: menuItems, onClick: ({ domEvent }) => domEvent.stopPropagation() }}
            trigger={['click']}
            placement="bottomRight"
          >
            <button type="button" className="jx-mySpace-moreBtn" onClick={(event) => event.stopPropagation()} title={t('更多操作')}>
              <MoreOutlined />
            </button>
          </Dropdown>
        )}
      </div>
    </div>
  );
}
