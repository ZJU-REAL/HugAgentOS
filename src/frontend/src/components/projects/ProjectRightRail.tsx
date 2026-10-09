import { prepareProjectFolderDeletion } from './projectFolderDeletion';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Dropdown, Empty, Input, Modal, Progress, Spin, Tag, message } from 'antd';
import {
  EditOutlined,
  ExportOutlined,
  FileTextOutlined,
  FolderAddOutlined,
  FolderOutlined,
  PlusOutlined,
} from '@ant-design/icons';
import ProjectFileList from './ProjectFileList';
import { fmtBytes, leafName } from './projectFileDisplay';
import type { ProjectFileItem } from '../../types';
import { useProjectStore } from '../../stores/projectStore';
import { FilePreviewPane } from '../file/FilePreviewPane';
import { DropOverlay } from '../common/DropOverlay';
import { UploadProgressBar } from '../common/UploadProgressBar';
import ProjectMemoryCard from './ProjectMemoryCard';
import { useFileDropZone } from '../../hooks/useFileDropZone';
import { t } from '../../i18n';

function InstructionsEditModal({
  initial, revision, open, onClose, onSave,
}: {
  initial: string;
  revision?: string;
  open: boolean;
  onClose: () => void;
  onSave: (v: string, revision?: string) => Promise<void>;
}) {
  const [draft, setDraft] = useState(initial);
  const [initialRevision] = useState(revision);
  const [saving, setSaving] = useState(false);
  const bytes = new TextEncoder().encode(draft).length;
  return (
    <Modal
      title={t('编辑项目指令')}
      width={880}
      style={{ top: 32 }}
      open={open}
      onCancel={onClose}
      confirmLoading={saving}
      okButtonProps={{ disabled: bytes > 32768 }}
      onOk={async () => {
        if (saving) return;
        setSaving(true);
        try {
          await onSave(draft, initialRevision);
          message.success(t('已保存'));
          onClose();
        } catch (err) {
          message.error((err as Error)?.message || t('保存失败'));
        } finally {
          setSaving(false);
        }
      }}
      okText={t('保存')}
      cancelText={t('取消')}
    >
      <p>{t('保存后同步到项目根目录 AGENTS.md，后续对话自动读取。')}</p>
      <Input.TextArea
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        rows={18}
        style={{ height: 'min(55vh, 560px)', minHeight: 240, resize: 'vertical' }}
        showCount={{ formatter: () => `${bytes} / 32768 B` }}
        status={bytes > 32768 ? 'error' : undefined}
        placeholder={t('为本项目的对话设定基调、目标、必须遵守的规则等…')}
      />
    </Modal>
  );
}

function InstructionsCard() {
  const project = useProjectStore((s) => s.currentProject);
  const setOpen = useProjectStore((s) => s.setInstructionsEditOpen);
  const open = useProjectStore((s) => s.instructionsEditOpen);
  const updateInstructions = useProjectStore((s) => s.updateInstructions);
  const refreshInstructions = useProjectStore((s) => s.refreshInstructions);
  const [syncError, setSyncError] = useState('');
  const projectId = project?.project_id;

  useEffect(() => {
    if (!projectId) return;
    let disposed = false;
    let refreshing = false;
    const refresh = async () => {
      if (document.hidden || refreshing) return;
      refreshing = true;
      try {
        await refreshInstructions();
        if (!disposed) setSyncError('');
      } catch (err) {
        if (!disposed) setSyncError((err as Error).message || t('加载失败'));
      } finally {
        refreshing = false;
      }
    };
    const timer = window.setInterval(() => { void refresh(); }, 10000);
    window.addEventListener('focus', refresh);
    return () => {
      disposed = true;
      window.clearInterval(timer);
      window.removeEventListener('focus', refresh);
    };
  }, [projectId, refreshInstructions]);

  const canEdit = project?.permission === 'admin' || project?.permission === 'edit';

  return (
    <div className="jx-projectRail-card">
      <div className="jx-projectRail-cardHeader">
        <div className="jx-projectRail-cardTitle">{t('项目指令')}</div>
        {canEdit && (
          <Button
            type="text"
            icon={<EditOutlined />}
            size="small"
            onClick={async () => {
              try {
                await refreshInstructions();
                setSyncError('');
                setOpen(true);
              } catch (err) {
                message.error((err as Error).message || t('加载失败'));
              }
            }}
          />
        )}
      </div>
      <div className="jx-projectRail-cardEmpty">
        {t('与项目根目录 AGENTS.md 同步；输入 /init 可初始化指令。')}
      </div>
      {syncError && <div role="alert">{syncError}</div>}
      {project?.instructions ? (
        <div className="jx-projectRail-cardText">{project.instructions}</div>
      ) : (
        <div className="jx-projectRail-cardEmpty">
          {t('为本项目添加指令，让 AI 更贴合任务需求。')}
        </div>
      )}

      {open && (
        <InstructionsEditModal
          key={`instr-${project?.project_id}-${open ? '1' : '0'}`}
          initial={project?.instructions || ''}
          revision={project?.instructions_revision}
          open={open}
          onClose={() => setOpen(false)}
          onSave={updateInstructions}
        />
      )}
    </div>
  );
}

// ─── FilesCard: browse the hooked folder subtree ──────────────────────────────────────

function FilesCard() {
  const project = useProjectStore((s) => s.currentProject);
  const files = useProjectStore((s) => s.projectFiles);
  const filesLoading = useProjectStore((s) => s.filesLoading);
  const filesError = useProjectStore((s) => s.filesError);
  const refreshFiles = useProjectStore((s) => s.refreshFiles);
  const capacityUsed = useProjectStore((s) => s.capacityUsed);
  const capacityLimit = useProjectStore((s) => s.capacityLimit);
  const uploadFiles = useProjectStore((s) => s.uploadFiles);
  const removeFile = useProjectStore((s) => s.removeFile);
  const uploadProgress = useProjectStore((s) => s.uploadProgress);

  const canEdit = project?.permission === 'admin' || project?.permission === 'edit';
  // 本地文件夹项目：文件即本机真实文件，增删在访达/文件管理器里做——隐藏上传、
  // 删除与容量条，改为提供「在访达中打开」入口（项目文件卡片头部，+ 号位置旁）。
  const isLocal = (project as { kind?: string } | null)?.kind === 'local';
  const localPath = (project as { local_path?: string } | null)?.local_path || '';
  const canUpload = canEdit && !isLocal;
  const pct = capacityLimit > 0 ? Math.min(100, Math.round((capacityUsed / capacityLimit) * 100)) : 0;

  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const folderInputRef = useRef<HTMLInputElement | null>(null);
  const [previewFile, setPreviewFile] = useState<ProjectFileItem | null>(null);
  const runUpload = useCallback((picked: File[], kind: 'file' | 'folder') => {
    if (picked.length === 0) return;
    void (async () => {
      const { succeeded, failed } = await uploadFiles(picked);
      if (succeeded > 0) {
        message.success(
          kind === 'folder'
            ? (failed ? t('文件夹上传完成：成功 {n} 个，失败 {m}', { n: succeeded, m: failed }) : t('文件夹上传完成：成功 {n} 个', { n: succeeded }))
            : (failed === 0 ? t('已上传 {n} 个文件', { n: succeeded }) : t('已上传 {n} 个，失败 {m}', { n: succeeded, m: failed })),
        );
      } else {
        message.error(t('上传失败（{n} 个文件）', { n: failed }));
      }
    })();
  }, [uploadFiles]);

  const handleFileInput = (e: React.ChangeEvent<HTMLInputElement>, kind: 'file' | 'folder') => {
    const list = e.target.files;
    if (!list || list.length === 0) return;
    runUpload(Array.from(list), kind);
    e.target.value = '';
  };

  // ── Drag-and-drop upload (disabled without edit permission; overlay mounts only while dragging, so it does not swallow row clicks) ──
  const { dragActive, dropZoneProps } = useFileDropZone(
    canUpload,
    (dropped) => runUpload(Array.from(dropped), 'file'),
  );

  const doDelete = useCallback((f: ProjectFileItem) => {
    Modal.confirm({
      title: t('删除文件？'),
      content: t('将从项目和「我的空间」中同步软删除该文件。'),
      okType: 'danger',
      okText: t('删除'),
      cancelText: t('取消'),
      onOk: async () => {
        try {
          await removeFile(f.artifact_id);
          message.success(t('已删除'));
        } catch (err) {
          message.error((err as Error)?.message || t('删除失败'));
        }
      },
    });
  }, [removeFile]);

  const doDeleteFolder = useCallback((name: string) => {
    if (!project || !canUpload) return;
    void prepareProjectFolderDeletion(project, name).then(({ fileCount, remove }) => {
      Modal.confirm({
        title: t('删除文件夹「{name}」', { name }),
        content: t('该文件夹及其子目录内共有 {n} 个文件将一并被删除。此操作会级联软删，确认继续吗？', { n: fileCount }),
        okType: 'danger',
        okText: t('删除'),
        cancelText: t('取消'),
        onOk: async () => {
          try {
            await remove();
          } catch (err) {
            message.error((err as Error)?.message || t('删除失败'));
            throw err;
          }
          message.success(t('已删除'));
          // A refresh failure has its own file-list retry; deletion already succeeded.
          await refreshFiles().catch(() => {});
        },
      });
    }).catch((err) => {
      message.error((err as Error)?.message || t('删除失败'));
    });
  }, [project, canUpload, refreshFiles]);

  // Adapt ProjectFileItem to the ResourceItem shape that FilePreviewPane accepts
  const previewItem = previewFile
    ? {
        id: previewFile.artifact_id,
        origin: isLocal ? 'local' : 'cloud',
        file_id: previewFile.artifact_id,
        name: leafName(previewFile),
        mime_type: previewFile.mime_type,
        size: previewFile.size_bytes,
        download_url: previewFile.download_url,
        type: (previewFile.mime_type || '').startsWith('image/') ? 'image' : 'document',
        created_at: previewFile.created_at || '',
      } as unknown as import('../../types').ResourceItem
    : null;

  const folderTag = project?.folder_name ? (
    <Tag color="blue" style={{ marginLeft: 4 }}>
      <FolderOutlined style={{ marginRight: 4 }} />{project.folder_name}
    </Tag>
  ) : null;

  return (
    <div className="jx-projectRail-card" {...dropZoneProps}>
      <div className="jx-projectRail-cardHeader">
        <div className="jx-projectRail-cardTitle">
          {t('项目文件')}{folderTag}
        </div>
        <div className="jx-projectRail-cardHeaderRight">
          {isLocal && localPath && (
            <Button
              type="text"
              size="small"
              icon={<ExportOutlined />}
              title={t('在访达 / 文件管理器中打开项目文件夹：{path}', { path: localPath })}
              onClick={() => {
                window.location.href = '/__desktop/open-path?path=' + encodeURIComponent(localPath);
              }}
            />
          )}
          {canUpload && (
            <Dropdown
              trigger={['click']}
              menu={{
                items: [
                  {
                    key: 'upload-file',
                    icon: <FileTextOutlined />,
                    label: t('上传文件'),
                    onClick: () => fileInputRef.current?.click(),
                  },
                  {
                    key: 'upload-folder',
                    icon: <FolderAddOutlined />,
                    label: t('上传文件夹'),
                    onClick: () => folderInputRef.current?.click(),
                  },
                ],
              }}
            >
              <Button type="text" icon={<PlusOutlined />} size="small" title={t('添加文件 / 文件夹')} />
            </Dropdown>
          )}
        </div>
      </div>

      {isLocal ? (
        <div className="jx-projectRail-cardSub" title={localPath}>{localPath}</div>
      ) : (
        <>
          <Progress percent={pct} size="small" showInfo={false} strokeColor="#126DFF" />
          <div className="jx-projectRail-cardSub">
            {t('已用 {used} / {limit}', { used: fmtBytes(capacityUsed), limit: fmtBytes(capacityLimit || 0) })}
          </div>
        </>
      )}

      {/* Thin progress bar for batch upload (spring-follows, fades out with delay on completion; the n/N label sits to the right of the bar) */}
      <UploadProgressBar progress={uploadProgress} />

      {filesError && (
        <div role="alert">
          {filesError}
          <Button size="small" onClick={() => { void refreshFiles().catch(() => {}); }}>{t('重试')}</Button>
        </div>
      )}
      {filesLoading && files.length === 0 ? <Spin /> : files.length === 0 && !filesError ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('该项目还没有文件')} />
      ) : (
        <ProjectFileList key={project?.project_id} files={files} canEdit={canUpload}
          onPreview={setPreviewFile} onDelete={doDelete} onDeleteFolder={doDeleteFolder} />
      )}

      <input
        ref={fileInputRef}
        type="file"
        multiple
        style={{ display: 'none' }}
        onChange={(e) => handleFileInput(e, 'file')}
      />
      <input
        ref={folderInputRef}
        type="file"
        // @ts-expect-error webkitdirectory is a non-standard DOM attribute; supported by Chromium/Firefox/Safari
        webkitdirectory=""
        directory=""
        multiple
        style={{ display: 'none' }}
        onChange={(e) => handleFileInput(e, 'folder')}
      />

      <Modal
        open={!!previewFile}
        onCancel={() => setPreviewFile(null)}
        footer={null}
        width="min(1100px, 90vw)"
        title={previewFile ? leafName(previewFile) : t('文件预览')}
        destroyOnClose
        style={{ top: 24 }}
        styles={{ body: { padding: 0, height: '82vh', display: 'flex' } }}
      >
        <div className="jx-projectRail-previewWrap">
          <FilePreviewPane item={previewItem} />
        </div>
      </Modal>

      {/* Drag-and-drop upload highlight layer (shared component, mounts only while dragging) */}
      <DropOverlay
        active={dragActive && canUpload}
        className="jx-projectRail-dropOverlay"
        iconSize={20}
        hint={t('松开，上传到本项目')}
      />
    </div>
  );
}

export default function ProjectRightRail() {
  const project = useProjectStore((s) => s.currentProject);
  if (!project) return null;
  return (
    <div className="jx-projectRail">
      <ProjectMemoryCard projectId={project.project_id} />
      <InstructionsCard />
      <FilesCard />
    </div>
  );
}
