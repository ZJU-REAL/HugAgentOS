import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Dropdown, Empty, Input, Modal } from 'antd';
import { ArrowLeftOutlined, DownOutlined, FolderAddOutlined, ReloadOutlined, SafetyOutlined, SearchOutlined, UploadOutlined } from '@ant-design/icons';
import { SidebarToggleIcon } from './SidebarToggleIcon';
import type { PersonalFolderNode, ResourceItem } from '../../types';
import { t } from '../../i18n';
import { AssetTypeSwitch } from './AssetTypeSwitch';
import { DocumentList } from './DocumentList';
import { SpaceImageGrid } from './SpaceImageGrid';
import { MySpaceSkeleton } from './MySpaceSkeleton';
import { SpacePermissions } from './SpacePermissions';
import { useSpaceCanvas } from './useSpaceCanvas';
import { useDelayedFlag } from '../../hooks';
import { SpaceDirectoryTree } from './SpaceDirectoryTree';
import { useSpaceDirectory } from './useSpaceDirectory';
import { createSpaceDirectory, deleteSpaceEntry, downloadSpaceFile, renameSpaceFile, uploadSpaceFile, type SpaceEntry } from './spaceFilesApi';

const joinPath = (parent: string, name: string) => parent ? parent + '/' + name : name;
const imageName = /\.(png|jpe?g|gif|webp|bmp|svg|avif|ico)$/i;

export function SpaceFileBrowser({ kind }: { kind: 'personal' | 'team' }) {
  const [query, setQuery] = useState('');
  const s = useSpaceDirectory(kind, query);
  const { roots, selected, path, setPath, page, loading, initializing, busy, error, root,
    writable, reload, perform, loadMore, dialog, setDialog, name, setName, permissions, setPermissions } = s;
  const [filter, setFilter] = useState<'document' | 'image'>('document');
  // Fast listings skip the skeleton entirely; a shown skeleton stays long enough not to flicker.
  const pending = (loading || initializing) && !page;
  const showSkeleton = useDelayedFlag(pending);
  const openFile = useSpaceCanvas();
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  // collapsed: docked sidebar hidden by the user; drawerOpen: sidebar shown as an overlay when the workspace is narrow
  const [collapsed, setCollapsed] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [pagedAt, setPagedAt] = useState('');
  const fileInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!drawerOpen) return;
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape') setDrawerOpen(false); };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [drawerOpen]);
  const folderInput = useRef<HTMLInputElement>(null);
  const entries = page?.entries ?? [];
  const folders: PersonalFolderNode[] = entries.filter(entry => entry.is_directory).map(entry => ({
    folder_id: entry.path, parent_folder_id: path || null, name: entry.name, created_at: entry.modified_at, children: [],
  }));
  const files: ResourceItem[] = entries.filter(entry => !entry.is_directory && (filter === 'image' ? imageName.test(entry.name) : !imageName.test(entry.name))).map(entry => ({
    id: entry.path, file_id: selected + ':' + entry.path, name: entry.name,
    type: imageName.test(entry.name) ? 'image' : 'document', size: entry.size,
    created_at: entry.modified_at || '', origin: 'cloud',
    download_url: '/v1/myspace/content?' + new URLSearchParams({ space: selected, path: entry.path }),
  }));
  const entryFor = (id: string) => page?.entries.find(entry => entry.path === id);
  function rename(id: string) {
    const entry = entryFor(id);
    if (entry && writable) { setName(entry.name); setDialog(entry); }
  }
  function remove(id: string, confirm = false) {
    const entry = entryFor(id);
    if (!entry || !writable) return;
    const run = () => perform(() => deleteSpaceEntry(selected, entry));
    if (confirm) Modal.confirm({ title: t('确认删除'), content: entry.name, onOk: run });
    else void run();
  }
  function download(item: ResourceItem) {
    const entry = entryFor(item.id);
    if (entry) void perform(() => downloadSpaceFile(selected, entry));
  }
  async function upload(files: File[]) {
    if (!writable || !files.length) return;
    setProgress({ done: 0, total: files.length });
    try {
      await perform(async () => {
        const made = new Set<string>();
        for (let i = 0; i < files.length; i++) {
          const file = files[i];
          const relative = file.webkitRelativePath || file.name;
          const parts = relative.split('/');
          for (let depth = 1; depth < parts.length; depth++) {
            const directory = joinPath(path, parts.slice(0, depth).join('/'));
            if (!made.has(directory)) { await createSpaceDirectory(selected, directory); made.add(directory); }
          }
          await uploadSpaceFile(selected, joinPath(path, relative), file);
          setProgress({ done: i + 1, total: files.length });
        }
      });
    } finally { setProgress(null); }
  }
  const parts = path ? path.split('/') : [];
  const location = selected + ':' + path;
  const crumbs = [{ name: root?.name ?? '', path: '' }, ...parts.map((part, index) => ({ name: part, path: parts.slice(0, index + 1).join('/') }))];
  // Deep paths keep the team, the parent and the current folder; the middle levels fold into "…".
  const shownCrumbs = crumbs.length > 3 ? [crumbs[0], { name: '…', path: crumbs[crumbs.length - 3].path }, ...crumbs.slice(-2)] : crumbs;
  const uploadMenu = { items: [
    { key: 'file', label: t('上传文件'), onClick: () => fileInput.current?.click() },
    { key: 'folder', label: t('上传文件夹'), onClick: () => folderInput.current?.click() },
  ] };
  const uploadLabel = progress ? t('上传中 {done}/{total}', progress) : t(filter === 'image' ? '上传图片' : '上传文件');
  const content = <>
    {kind === 'team' && <div className="jx-team-workspace-toolbar">
      <Button type="text" className="jx-team-sidebarToggle jx-team-sidebarToggle--docked" aria-label={t('展开目录')}
        title={t('展开目录')} icon={<SidebarToggleIcon />} onClick={() => setCollapsed(false)} />
      <Button type="text" className="jx-team-sidebarToggle jx-team-sidebarToggle--drawer" aria-label={t('展开目录')}
        aria-expanded={drawerOpen} title={t('展开目录')} icon={<SidebarToggleIcon />} onClick={() => setDrawerOpen(true)} />
      <nav className="jx-team-crumbs" aria-label={t('当前位置')}>
        {shownCrumbs.map((crumb, index) => index === shownCrumbs.length - 1
          ? <span key={crumb.path} className="jx-team-crumb jx-team-crumb--current" title={crumb.name} aria-current="page">{crumb.name}</span>
          : <span key={crumb.path + ':' + index} className="jx-team-crumbItem">
              <button type="button" className="jx-team-crumb" title={crumb.name} onClick={() => setPath(crumb.path)}>{crumb.name}</button>
              <span className="jx-team-crumbSep" aria-hidden="true">/</span>
            </span>)}
      </nav>
      <div className="jx-team-workspace-actions">
        {root?.permission === 'view' && <span className="jx-team-workspace-readOnly">{t('仅可查看')}</span>}
        {writable && <>
          <Button icon={<FolderAddOutlined />} disabled={busy} title={t('新建文件夹')} aria-label={t('新建文件夹')}
            onClick={() => { setName(''); setDialog('directory'); }}>
            <span className="jx-team-toolbar-label">{t('新建文件夹')}</span>
          </Button>
          <Dropdown trigger={['click']} menu={uploadMenu}>
            <Button type="primary" icon={<UploadOutlined />} loading={busy} title={uploadLabel} aria-label={uploadLabel}>
              <span className="jx-team-toolbar-label">{uploadLabel}</span><DownOutlined />
            </Button>
          </Dropdown>
        </>}
        <Button type="text" aria-label={t('刷新')} title={t('刷新')} icon={<ReloadOutlined />} disabled={busy} onClick={reload} />
        {root?.permission === 'admin' && <>
          <span className="jx-team-toolbar-divider" aria-hidden="true" />
          <Button type="text" icon={<SafetyOutlined />} title={t('管理成员权限')} aria-label={t('管理成员权限')} onClick={() => setPermissions(true)}>
            <span className="jx-team-toolbar-label">{t('成员权限')}</span>
          </Button>
        </>}
      </div>
    </div>}
    {error && <Alert type="error" showIcon message={error} />}
    {showSkeleton ? <MySpaceSkeleton tab="assets" assetFilter={filter} />
      : pending ? <div aria-busy="true" />
      : !selected || (!files.length && (filter === 'image' || !folders.length)) ? <div className="jx-mySpace-empty"><Empty description={t('当前文件夹暂无文件')} /></div>
      : filter === 'image' ? <SpaceImageGrid items={files} onPreview={openFile} onDownload={download}
          onRename={writable && !busy ? item => rename(item.id) : undefined}
          onDelete={writable && !busy ? item => remove(item.id, true) : undefined} />
      : <DocumentList key={selected + ':' + path} items={files} folders={folders}
          onEnterFolder={setPath} onPreview={openFile} onDownload={download}
          onRename={writable && !busy ? item => rename(item.id) : undefined}
          onDelete={writable && !busy ? item => remove(item.id) : undefined}
          onRenameFolder={writable && !busy ? rename : undefined}
          onDeleteFolder={writable && !busy ? id => remove(id, true) : undefined} />}
    {page?.next_cursor && <div className="jx-mySpace-loadMore"><Button loading={loading} onClick={() => { setPagedAt(location); void loadMore(); }}>{t('加载更多')}</Button></div>}
    {page && !page.next_cursor && pagedAt === location && entries.length > 0 && <div className="jx-mySpace-noMore">{t('已加载全部内容')}</div>}
  </>;
  return <section aria-label={t('文件')} onDragOver={event => { if (writable) event.preventDefault(); }}
    onDrop={event => { if (writable) { event.preventDefault(); void upload(Array.from(event.dataTransfer.files)); } }}>
    <div className="jx-mySpace-assetBar">
      <AssetTypeSwitch value={filter} onChange={setFilter} />
      <div className="jx-mySpace-filterTools">
        {kind === 'personal' && path && <Button type="text" icon={<ArrowLeftOutlined />} onClick={() => setPath(parts.slice(0, -1).join('/'))}>{t('返回上级')}</Button>}
        {kind === 'personal' && writable && <>
          <Button icon={<FolderAddOutlined />} disabled={busy} onClick={() => { setName(''); setDialog('directory'); }}>{t('新建文件夹')}</Button>
          <Dropdown trigger={['click']} menu={{ items: [
            { key: 'file', label: t('上传文件'), onClick: () => fileInput.current?.click() },
            { key: 'folder', label: t('上传文件夹'), onClick: () => folderInput.current?.click() },
          ] }}>
            <Button type="primary" icon={<UploadOutlined />} loading={busy}>
              {progress ? t('上传中 {done}/{total}', progress) : t(filter === 'image' ? '上传图片' : '上传文件')}<DownOutlined />
            </Button>
          </Dropdown>
        </>}
        {kind === 'personal' && <Button type="text" aria-label={t('刷新')} icon={<ReloadOutlined />} disabled={busy} onClick={reload} />}
        <Input className="jx-mySpace-search" placeholder={t('搜索')} prefix={<SearchOutlined />} value={query} allowClear onChange={event => setQuery(event.target.value)} />
      </div>
    </div>
    <input ref={fileInput} type="file" multiple hidden accept={filter === 'image' ? 'image/*' : undefined}
      onChange={event => { void upload(Array.from(event.target.files ?? [])); event.target.value = ''; }} />
    <input ref={folderInput} type="file" multiple hidden {...{ webkitdirectory: '', directory: '' }}
      onChange={event => { void upload(Array.from(event.target.files ?? [])); event.target.value = ''; }} />
    {kind === 'team' ? <div className="jx-team-workspaceFrame">
      <div className={'jx-team-workspace' + (collapsed ? ' jx-team-workspace--collapsed' : '') + (drawerOpen ? ' jx-team-workspace--drawerOpen' : '')}>
        <div className="jx-team-workspace-scrim" aria-hidden="true" onClick={() => setDrawerOpen(false)} />
        <aside className="jx-team-workspace-sidebar" aria-label={t('团队目录')}>
          <div className="jx-team-workspace-sidebarHead">
            <span className="jx-team-workspace-sidebarTitle">{t('团队空间 · {n}', { n: roots.length })}</span>
            <Button type="text" size="small" className="jx-team-sidebarToggle jx-team-sidebarToggle--collapse" aria-label={t('收起目录')}
              title={t('收起目录')} icon={<SidebarToggleIcon />} onClick={() => setCollapsed(true)} />
            <Button type="text" size="small" className="jx-team-sidebarToggle jx-team-sidebarToggle--close" aria-label={t('关闭目录')}
              title={t('关闭目录')} icon={<SidebarToggleIcon />} onClick={() => setDrawerOpen(false)} />
          </div>
          <SpaceDirectoryTree roots={roots} selected={selected} path={path} revision={s.revision}
            onSelect={(space, next) => {
              // Re-clicking the current folder only toggles it, so keep the drawer open for that.
              if (space !== selected || next !== path) setDrawerOpen(false);
              s.open(space, next);
            }} />
        </aside><section className="jx-team-workspace-main">{content}</section>
      </div>
    </div> : content}
    <Modal open={dialog !== null} title={t(dialog === 'directory' ? '新建文件夹' : '重命名')}
      onCancel={() => setDialog(null)} confirmLoading={busy}
      okButtonProps={{ disabled: !name.trim() || /[/\\]/.test(name) || name === '.' || name === '..' }}
      onOk={() => void perform(() => dialog === 'directory'
        ? createSpaceDirectory(selected, joinPath(path, name.trim()))
        : renameSpaceFile(selected, (dialog as SpaceEntry).path, joinPath(path, name.trim())))}>
      <Input aria-label={t('名称')} value={name} onChange={event => setName(event.target.value)} />
    </Modal>
    {root?.kind === 'team' && <SpacePermissions open={permissions} teamId={root.space_id.slice(5)} onClose={() => { setPermissions(false); reload(); }} />}
  </section>;
}
