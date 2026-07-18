import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, Dropdown, Empty, Input, Modal, Popover, Progress, Switch, Tag, Tooltip, message } from 'antd';
import {
  CaretRightOutlined,
  DeleteOutlined,
  EditOutlined,
  EyeOutlined,
  FileTextOutlined,
  FolderAddOutlined,
  FolderOutlined,
  PlusOutlined,
  SettingOutlined,
} from '@ant-design/icons';
import type { ProjectFileItem } from '../../types';
import { useProjectStore } from '../../stores/projectStore';
import { FilePreviewPane } from '../file/FilePreviewPane';
import { DropOverlay } from '../common/DropOverlay';
import { UploadProgressBar } from '../common/UploadProgressBar';
import ProjectMemoriesModal from './ProjectMemoriesModal';
import { useFileDropZone } from '../../hooks/useFileDropZone';
import { t } from '../../i18n';

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

/** Use the file extension as a short type label (do not show the verbose mime). */
function shortType(item: ProjectFileItem): string {
  const name = item.name || '';
  const idx = name.lastIndexOf('.');
  if (idx > 0 && idx < name.length - 1) {
    return name.slice(idx + 1).toUpperCase();
  }
  const mime = item.mime_type || '';
  if (mime.startsWith('image/')) return mime.slice(6).toUpperCase();
  if (mime === 'application/pdf') return 'PDF';
  return t('文件');
}

/** Display name for a file inside the project: strip the folder_path prefix, keep only the file name itself. */
function leafName(item: ProjectFileItem): string {
  const i = item.name.lastIndexOf('/');
  return i === -1 ? item.name : item.name.slice(i + 1);
}

// ─── Memory + Instructions cards (reusing the previous implementation) ────────────────────────

function MemoryCard({ projectId }: { projectId: string }) {
  const project = useProjectStore((s) => s.currentProject);
  const updateProject = useProjectStore((s) => s.updateProject);
  const readEnabled = project?.memory_enabled ?? true;
  const writeEnabled = project?.memory_write_enabled ?? true;
  const canEdit = project?.permission === 'admin' || project?.permission === 'edit';

  const [count, setCount] = useState<number | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [savingRead, setSavingRead] = useState(false);
  const [savingWrite, setSavingWrite] = useState(false);
  const [viewerOpen, setViewerOpen] = useState(false);

  useEffect(() => {
    let aborted = false;
    void (async () => {
      try {
        const { getApiUrl } = await import('../../api');
        const resp = await fetch(
          `${getApiUrl()}/v1/memories?project_id=${encodeURIComponent(projectId)}`,
          { credentials: 'include' },
        );
        const payload = await resp.json();
        if (aborted) return;
        const data = payload?.data || {};
        setCount(typeof data.count === 'number' ? data.count : 0);
      } catch {
        if (!aborted) setCount(0);
      }
    })();
    return () => { aborted = true; };
  }, [projectId, reloadKey, readEnabled]);

  const toggle = async (kind: 'read' | 'write', next: boolean) => {
    const setSaving = kind === 'read' ? setSavingRead : setSavingWrite;
    setSaving(true);
    try {
      await updateProject(
        kind === 'read' ? { memory_enabled: next } : { memory_write_enabled: next },
      );
      setReloadKey((k) => k + 1);
    } catch (err) {
      message.error((err as Error)?.message || t('保存失败'));
    } finally {
      setSaving(false);
    }
  };

  const settingsContent = (
    <div className="jx-projectRail-memoryToggles">
      <Tooltip title={t('关闭后，本项目内对话不会检索 / 注入项目记忆')} placement="left">
        <div className="jx-projectRail-memoryToggleRow">
          <span className="jx-projectRail-memoryToggleLabel">{t('读取记忆')}</span>
          <Switch
            size="small"
            checked={readEnabled}
            loading={savingRead}
            disabled={!canEdit}
            onChange={(v) => toggle('read', v)}
          />
        </div>
      </Tooltip>
      <Tooltip title={t('关闭后，本项目内会话结束不会抽取并写入新的项目记忆')} placement="left">
        <div className="jx-projectRail-memoryToggleRow">
          <span className="jx-projectRail-memoryToggleLabel">{t('写入记忆')}</span>
          <Switch
            size="small"
            checked={writeEnabled}
            loading={savingWrite}
            disabled={!canEdit}
            onChange={(v) => toggle('write', v)}
          />
        </div>
      </Tooltip>
    </div>
  );

  return (
    <div className="jx-projectRail-card">
      <div className="jx-projectRail-cardHeader">
        <div className="jx-projectRail-cardTitle">{t('项目记忆')}</div>
        <div className="jx-projectRail-cardHeaderRight">
          <span className="jx-projectRail-cardAux">{t('仅本项目可见')}</span>
          <Popover
            content={settingsContent}
            title={t('项目记忆设置')}
            trigger="click"
            placement="bottomRight"
            overlayClassName="jx-projectRail-memoryPopover"
          >
            <Button
              type="text"
              size="small"
              icon={<SettingOutlined />}
              title={t('项目记忆设置')}
            />
          </Popover>
        </div>
      </div>

      {!readEnabled ? (
        <div className="jx-projectRail-cardEmpty">{t('读取已关闭，项目记忆不会注入对话')}</div>
      ) : count === null ? (
        <div className="jx-projectRail-cardEmpty">{t('加载中…')}</div>
      ) : count === 0 ? (
        <div className="jx-projectRail-cardEmpty">{t('几轮对话之后，项目记忆会出现在这里。')}</div>
      ) : (
        <div
          className="jx-projectRail-cardEmpty jx-projectRail-memoryCount"
          onClick={() => setViewerOpen(true)}
          style={{ cursor: 'pointer' }}
          title={t('点击查看项目记忆详情')}
        >
          {t('已积累 {n} 条记忆 · ', { n: count })}<span style={{ color: 'var(--color-primary)' }}>{t('查看')}</span>
        </div>
      )}

      <ProjectMemoriesModal
        open={viewerOpen}
        projectId={projectId}
        projectName={project?.name}
        onClose={() => setViewerOpen(false)}
        onChange={() => setReloadKey((k) => k + 1)}
      />
    </div>
  );
}

function InstructionsEditModal({
  initial,
  open,
  onClose,
  onSave,
}: {
  initial: string;
  open: boolean;
  onClose: () => void;
  onSave: (v: string) => Promise<void>;
}) {
  const [draft, setDraft] = useState(initial);
  return (
    <Modal
      title={t('编辑项目指令')}
      open={open}
      onCancel={onClose}
      onOk={async () => {
        try {
          await onSave(draft);
          message.success(t('已保存'));
          onClose();
        } catch (err) {
          message.error((err as Error)?.message || t('保存失败'));
        }
      }}
      okText={t('保存')}
      cancelText={t('取消')}
    >
      <Input.TextArea
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        rows={10}
        maxLength={8000}
        showCount
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
            onClick={() => setOpen(true)}
          />
        )}
      </div>
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
          open={open}
          onClose={() => setOpen(false)}
          onSave={updateInstructions}
        />
      )}
    </div>
  );
}

// ─── FilesCard: browse the hooked folder subtree ──────────────────────────────────────

interface FileRowProps {
  file: ProjectFileItem;
  indent: boolean;
  canEdit: boolean;
  onPreview: () => void;
  onDelete: () => void;
}

function FileRow({ file, indent, canEdit, onPreview, onDelete }: FileRowProps) {
  return (
    <div
      className={`jx-projectRail-fileItem jx-projectRail-fileItem--clickable${indent ? ' jx-projectRail-fileItem--indent' : ''}`}
      onClick={onPreview}
      title={t('点击预览')}
    >
      <div className="jx-projectRail-fileInfo">
        <div className="jx-projectRail-fileName" title={file.name}>{leafName(file)}</div>
        <div className="jx-projectRail-fileMeta">
          {shortType(file)} · {fmtBytes(file.size_bytes || 0)}
        </div>
      </div>
      <div style={{ display: 'flex', gap: 2, flexShrink: 0 }}>
        <Button
          type="text"
          size="small"
          icon={<EyeOutlined />}
          onClick={(e) => { e.stopPropagation(); onPreview(); }}
          title={t('预览')}
        />
        {canEdit && (
          <Button
            type="text"
            size="small"
            icon={<DeleteOutlined />}
            onClick={(e) => { e.stopPropagation(); onDelete(); }}
            title={t('删除')}
          />
        )}
      </div>
    </div>
  );
}

function FilesCard() {
  const project = useProjectStore((s) => s.currentProject);
  const files = useProjectStore((s) => s.projectFiles);
  const capacityUsed = useProjectStore((s) => s.capacityUsed);
  const capacityLimit = useProjectStore((s) => s.capacityLimit);
  const uploadFiles = useProjectStore((s) => s.uploadFiles);
  const removeFile = useProjectStore((s) => s.removeFile);
  const uploadProgress = useProjectStore((s) => s.uploadProgress);

  const canEdit = project?.permission === 'admin' || project?.permission === 'edit';
  const pct = capacityLimit > 0 ? Math.min(100, Math.round((capacityUsed / capacityLimit) * 100)) : 0;

  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const folderInputRef = useRef<HTMLInputElement | null>(null);
  const [previewFile, setPreviewFile] = useState<ProjectFileItem | null>(null);
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set());

  /** Aggregate using the first segment of folder_path as the group key (deeper levels stay flattened within that group). */
  const grouped = useMemo(() => {
    const groups = new Map<string, ProjectFileItem[]>();
    const loose: ProjectFileItem[] = [];
    for (const f of files) {
      const path = f.folder_path || '';
      if (!path) {
        loose.push(f);
        continue;
      }
      const top = path.split('/', 1)[0];
      const arr = groups.get(top) || [];
      arr.push(f);
      groups.set(top, arr);
    }
    return { groups, loose };
  }, [files]);

  const toggleGroup = (key: string) => {
    setCollapsedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

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
    canEdit,
    (dropped) => runUpload(Array.from(dropped), 'file'),
  );

  const doDelete = (f: ProjectFileItem) => {
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
  };

  // Adapt ProjectFileItem to the ResourceItem shape that FilePreviewPane accepts
  const previewItem = previewFile
    ? {
        id: previewFile.artifact_id,
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
        {canEdit && (
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

      <Progress percent={pct} size="small" showInfo={false} strokeColor="#126DFF" />
      <div className="jx-projectRail-cardSub">
        {t('已用 {used} / {limit}', { used: fmtBytes(capacityUsed), limit: fmtBytes(capacityLimit || 0) })}
      </div>

      {/* Thin progress bar for batch upload (spring-follows, fades out with delay on completion; the n/N label sits to the right of the bar) */}
      <UploadProgressBar progress={uploadProgress} />

      {files.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('该项目还没有文件')} />
      ) : (
        <div className="jx-projectRail-fileList">
          {Array.from(grouped.groups.entries()).map(([groupName, items]) => {
            const isCollapsed = collapsedGroups.has(groupName);
            return (
              <div key={`g:${groupName}`} className="jx-projectRail-group">
                <div
                  className="jx-projectRail-groupHeader"
                  onClick={() => toggleGroup(groupName)}
                >
                  {/* Single-icon rotate transition, replacing the hard swap between the two CaretRight/Down icons */}
                  <CaretRightOutlined
                    className={`jx-projectRail-groupCaret${isCollapsed ? '' : ' jx-projectRail-groupCaret--open'}`}
                  />
                  <FolderOutlined style={{ color: 'var(--color-primary)' }} />
                  <span className="jx-projectRail-groupName" title={groupName}>{groupName}</span>
                  <span className="jx-projectRail-groupCount">{items.length}</span>
                </div>
                {/* Always rendered + grid-template-rows 0fr↔1fr height animation (analogous to jx-expandWrap) */}
                <div className={`jx-projectRail-groupBody${isCollapsed ? '' : ' jx-projectRail-groupBody--open'}`}>
                  <div className="jx-projectRail-groupBodyInner">
                    {items.map((f) => (
                      <FileRow
                        key={f.id}
                        file={f}
                        indent
                        canEdit={canEdit}
                        onPreview={() => setPreviewFile(f)}
                        onDelete={() => doDelete(f)}
                      />
                    ))}
                  </div>
                </div>
              </div>
            );
          })}
          {grouped.loose.map((f) => (
            <FileRow
              key={f.id}
              file={f}
              indent={false}
              canEdit={canEdit}
              onPreview={() => setPreviewFile(f)}
              onDelete={() => doDelete(f)}
            />
          ))}
        </div>
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
        active={dragActive && canEdit}
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
      <MemoryCard projectId={project.project_id} />
      <InstructionsCard />
      <FilesCard />
    </div>
  );
}
