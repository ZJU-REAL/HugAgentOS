import { AgentWorkspaceView } from '../../personalAgentEdition';
import { AssetTypeSwitch } from './AssetTypeSwitch';
import { Suspense } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import type { Variants } from 'motion/react';
import { ArrowLeftOutlined, DownOutlined, FileOutlined, FolderAddOutlined, FolderOpenOutlined, PictureOutlined, SearchOutlined, UploadOutlined } from '@ant-design/icons';
import { Button, Dropdown, Input, Modal, Select } from 'antd';

import { t } from '../../i18n';

import { EASE, SPRING } from '../../utils/motionTokens';
import { DropOverlay } from '../common/DropOverlay';
import { AssetUploadStatus } from './AssetUploadStatus';

import { CatalogPanel } from '../catalog';
import { ShareRecordsPage } from '../share';
import { DocumentList } from './DocumentList';
import { FavoriteList } from './FavoriteList';
import { ImageGrid } from './ImageGrid';
import { MySpaceSkeleton } from './MySpaceSkeleton';
import { NotificationList } from './NotificationList';
import { CreatePersonalFolderModal, MoveToPersonalFolderModal } from './personal';

import { useCeMySpaceController } from './useCeMySpaceController';
const scopeSlideVariants: Variants = {
  enter: (direction: number) => ({ opacity: 0, x: direction * 24 }),
  center: { opacity: 1, x: 0, transition: { duration: 0.22, ease: EASE.brandOut } },
  exit: (direction: number) => ({
    opacity: 0,
    x: direction * -24,
    transition: { duration: 0.16, ease: EASE.exit },
  }),
};

export function MySpacePanel() {
  const { visibleTabs, resources, favorites, loading, tab, searchKeyword, hasMore, assetFilter, sourceFilter, notifUnreadCount, selectedScope, personalChildFolders, setTab, setAssetFilter, setSourceFilter, deleteResource, removeFavorite, loadMore, enterPersonalFolder, fileInputRef, folderInputRef, imageInputRef, navDirection, uploadProgress, createFolderOpen, setCreateFolderOpen, moveFolderOpen, setMoveFolderOpen, moveArtifactIds, setMoveArtifactIds, moveMode, kbPickerOpen, setKbPickerOpen, kbPickerLoading, selectedKbIds, setSelectedKbIds, parentFolderId, handleSearch, handleFilesPicked, handleFolderPicked, handleRenameFolder, handleDeleteFolder, openMoveModal, handleDownload, handleNavigate, handlePreview, handleRequestUnfavorite, privateKbOptions, openKbPicker, handleAddToKb, canDropUpload, dragActive, dropZoneProps, showSkeleton, currentItems, currentHasMore, tabDescriptions, renderEmpty } = useCeMySpaceController();
  return (
    <div className="jx-mySpace" {...dropZoneProps}>
      <AssetUploadStatus />
      <div className="jx-mySpace-shell">
        <div className="jx-mySpace-header">
          <div className="jx-mySpace-tabs">
            {visibleTabs.map((item) => (
              <button
                key={item.key}
                className={`jx-mySpace-tab${tab === item.key ? ' active' : ''}`}
                onClick={() => { navDirection.current = 0; setTab(item.key); }}
              >
                <span>{item.label}</span>
                {item.key === 'notifications' && notifUnreadCount > 0 && (
                  <motion.span className="jx-mySpace-tabBadge" initial={{ scale: 0.6 }} animate={{ scale: 1 }}>
                    {notifUnreadCount > 99 ? '99+' : notifUnreadCount}
                  </motion.span>
                )}
                {tab === item.key && (
                  <motion.span layoutId="jx-mySpace-tabInk" className="jx-mySpace-tabInk" transition={SPRING.ink} />
                )}
              </button>
            ))}
          </div>
          <div className={`jx-mySpace-subHeader${tab === 'shares' ? ' jx-mySpace-subHeader-shares' : ''}`}>
            <p className="jx-mySpace-desc">{tabDescriptions[tab]}</p>
          </div>

          {tab === 'assets' && (
            <>
              <div className="jx-mySpace-assetBar">
                <AssetTypeSwitch value={assetFilter} onChange={value => { navDirection.current = 0; setAssetFilter(value); }} />
                <div className="jx-mySpace-filterTools">
                  {selectedScope.folderId !== null && (
                    <Button
                      type="text"
                      icon={<ArrowLeftOutlined />}
                      onClick={() => {
                        navDirection.current = -1;
                        void enterPersonalFolder(parentFolderId);
                      }}
                    >
                      {t('返回上级')}
                    </Button>
                  )}
                  {assetFilter === 'document' ? (
                    <>
                      <Button icon={<FolderAddOutlined />} onClick={() => setCreateFolderOpen(true)}>
                        {t('新建文件夹')}
                      </Button>
                      <Dropdown
                        trigger={['click']}
                        menu={{
                          items: [
                            { key: 'file', icon: <FileOutlined />, label: t('上传文件'), onClick: () => fileInputRef.current?.click() },
                            { key: 'folder', icon: <FolderOpenOutlined />, label: t('上传文件夹'), onClick: () => folderInputRef.current?.click() },
                          ],
                        }}
                      >
                        <Button type="primary" icon={<UploadOutlined />} loading={!!uploadProgress}>
                          {uploadProgress
                            ? t('上传中 {done}/{total}', {
                                done: uploadProgress.done,
                                total: uploadProgress.total,
                              })
                            : t('上传文件')}
                          {!uploadProgress && <DownOutlined style={{ marginLeft: 4, fontSize: 10 }} />}
                        </Button>
                      </Dropdown>
                    </>
                  ) : (
                    <Button type="primary" icon={<PictureOutlined />} onClick={() => imageInputRef.current?.click()}>
                      {t('上传图片')}
                    </Button>
                  )}
                  <Select
                    popupClassName="jx-mySpace-sourceFilterPopup"
                    className="jx-mySpace-sourceFilter"
                    value={sourceFilter}
                    onChange={setSourceFilter}
                    options={[
                      { value: 'all', label: t('全部来源') },
                      { value: 'user_upload', label: t('用户上传') },
                      { value: 'ai_generated', label: t('AI生成') },
                    ]}
                  />
                  <Input
                    className="jx-mySpace-search"
                    placeholder={t('搜索')}
                    prefix={<SearchOutlined style={{ color: 'var(--color-text-placeholder)' }} />}
                    value={searchKeyword}
                    onChange={(event) => handleSearch(event.target.value)}
                    allowClear
                  />
                </div>
              </div>
              <input ref={fileInputRef} type="file" multiple hidden onChange={(event) => {
                void handleFilesPicked(event.target.files);
                event.target.value = '';
              }} />
              <input
                ref={folderInputRef}
                type="file"
                // @ts-expect-error Chromium directory picker attribute.
                webkitdirectory=""
                directory=""
                multiple
                hidden
                onChange={(event) => {
                  void handleFolderPicked(event.target.files);
                  event.target.value = '';
                }}
              />
              <input ref={imageInputRef} type="file" accept="image/*" multiple hidden onChange={(event) => {
                void handleFilesPicked(event.target.files);
                event.target.value = '';
              }} />
            </>
          )}
        </div>

        {/* 知识库 Tab 由 CatalogPanel 自管分页，别让文件资产的无限滚动跟着触发 loadMore */}
        <div className="jx-mySpace-body" onScroll={(event) => {
          if (tab === 'shares' || tab === 'notifications' || tab === 'kb' || tab === 'agent') return;
          const element = event.currentTarget;
          if (element.scrollHeight - element.scrollTop - element.clientHeight < 100) void loadMore();
        }}>
          <motion.div key={tab} className="jx-mySpace-bodyFade" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
            {tab === 'agent' ? (
              <Suspense fallback={null}><AgentWorkspaceView /></Suspense>
            ) : tab === 'kb' ? (
              <CatalogPanel />
            ) : tab === 'notifications' ? (
              <NotificationList />
            ) : tab === 'shares' ? (
              <ShareRecordsPage embedded hideEmbeddedDesc />
            ) : tab === 'assets' ? (
              <AnimatePresence mode="popLayout" custom={navDirection.current} initial={false}>
                <motion.div
                  key={`${selectedScope.folderId ?? 'root'}:${assetFilter}`}
                  className="jx-mySpace-scopeSlide"
                  custom={navDirection.current}
                  variants={scopeSlideVariants}
                  initial="enter"
                  animate="center"
                  exit="exit"
                >
                  {showSkeleton ? (
                    <MySpaceSkeleton tab={tab} assetFilter={assetFilter} />
                  ) : resources.length === 0 && (assetFilter !== 'document' || personalChildFolders.length === 0) ? (
                    renderEmpty(t('当前文件夹暂无内容，先新建文件夹或上传文件试试'))
                  ) : assetFilter === 'document' ? (
                    <DocumentList
                      items={resources}
                      onDownload={handleDownload}
                      onNavigate={handleNavigate}
                      onDelete={(item) => { void deleteResource(item.id); }}
                      onPreview={handlePreview}
                      onAddToKb={openKbPicker}
                      folders={personalChildFolders}
                      onEnterFolder={(folderId) => {
                        navDirection.current = 1;
                        void enterPersonalFolder(folderId);
                      }}
                      onRenameFolder={handleRenameFolder}
                      onDeleteFolder={handleDeleteFolder}
                      onMoveToPersonalFolder={(item) => openMoveModal([item.id], 'move')}
                      onBulkMoveToPersonalFolder={(items) => openMoveModal(items.map((item) => item.id), 'move')}
                      onCopyToPersonalFolder={(item) => openMoveModal([item.id], 'copy')}
                      onBulkCopyToPersonalFolder={(items) => openMoveModal(items.map((item) => item.id), 'copy')}
                    />
                  ) : (
                    <ImageGrid
                      items={resources}
                      onDownload={handleDownload}
                      onNavigate={handleNavigate}
                      onDelete={(item) => { void deleteResource(item.id); }}
                      onMoveToPersonalFolder={(item) => openMoveModal([item.id], 'move')}
                      onCopyToPersonalFolder={(item) => openMoveModal([item.id], 'copy')}
                    />
                  )}
                  {!loading && !hasMore && resources.length > 0 && (
                    <div className="jx-mySpace-noMore">{t('已加载全部内容')}</div>
                  )}
                </motion.div>
              </AnimatePresence>
            ) : loading && currentItems.length === 0 ? (
              <MySpaceSkeleton tab={tab} assetFilter={assetFilter} />
            ) : currentItems.length === 0 ? (
              renderEmpty(t('暂无内容'))
            ) : (
              <>
                <FavoriteList
                  items={favorites}
                  onNavigate={handleNavigate}
                  onRequestUnfavorite={handleRequestUnfavorite}
                  onFinalizeUnfavorite={(item) => {
                    if (item.source_chat_id) removeFavorite(item.source_chat_id);
                  }}
                />
                {!loading && !currentHasMore && (
                  <div className="jx-mySpace-noMore">{t('已加载全部内容')}</div>
                )}
              </>
            )}
          </motion.div>
        </div>
      </div>

      <DropOverlay active={dragActive && canDropUpload} hint={t('松开，上传到当前文件夹')} />
      <CreatePersonalFolderModal open={createFolderOpen} onClose={() => setCreateFolderOpen(false)} />
      <MoveToPersonalFolderModal
        open={moveFolderOpen}
        mode={moveMode}
        artifactIds={moveArtifactIds}
        onClose={() => {
          setMoveFolderOpen(false);
          setMoveArtifactIds([]);
        }}
      />
      <Modal
        title={t('加入私有知识库')}
        open={kbPickerOpen}
        onCancel={() => setKbPickerOpen(false)}
        footer={[
          <Button key="cancel" onClick={() => setKbPickerOpen(false)}>{t('取消')}</Button>,
          <Button key="submit" type="primary" loading={kbPickerLoading} onClick={() => void handleAddToKb()}>
            {t('确认加入')}
          </Button>,
        ]}
      >
        <Select
          className="jx-mySpace-kbPickerSelect"
          mode="multiple"
          value={selectedKbIds}
          onChange={setSelectedKbIds}
          placeholder={t('请选择一个或多个私有知识库')}
          options={privateKbOptions.map((item) => ({ value: item.id, label: item.name }))}
        />
      </Modal>
    </div>
  );
}
