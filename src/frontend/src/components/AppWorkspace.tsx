import {
  CloseOutlined,
  InsertRowRightOutlined,
  MenuOutlined,
} from '@ant-design/icons';
import {
  Button,
  Layout,
  Modal,
  Tag,
  Tooltip,
  Typography
} from 'antd';
import 'highlight.js/styles/github.css';
import { lazy, Suspense, type CSSProperties } from 'react';
import { t } from '../i18n';

/* styles loaded via styles/index.ts in main.tsx */
import { useProjectStore } from '../stores/projectStore';
import { resolvePlanModeActive } from '../utils/chatMode';
import { TOPIC_TAG_COLORS } from '../utils/constants';
import { BatchConfirmModal } from './batch';
import { RightSidebarPanel } from './canvas';
import { ChatArea, PromptHubPanel } from './chat';
import { AuthExpiredModal, ImagePreview } from './common';
import { CollapseHeight } from './common/CollapseHeight';
import { CreateKBModal, ReindexModal } from './kb';
import { SearchModal, Sidebar } from './sidebar';
import { ToolResultPanel } from './tool';

import type { useAppController } from '../hooks/useAppController';
import { SlidePanel } from './common/SlidePanel';
const AutomationPanel = lazy(() => import('./automation/AutomationPanel').then(m => ({ default: m.AutomationPanel })));
const RunTimelinePanel = lazy(() => import('./automation/RunTimelinePanel').then(m => ({ default: m.RunTimelinePanel })));
const AbilityCenterPage = lazy(() => import('./catalog/AbilityCenterPage').then(m => ({ default: m.AbilityCenterPage })));
const AppCenterPanel = lazy(() => import('./docs/AppCenterPanel').then(m => ({ default: m.default })));
const DocsPanel = lazy(() => import('./docs/DocsPanel').then(m => ({ default: m.default })));
const LabPanel = lazy(() => import('./lab/LabPanel').then(m => ({ default: m.default })));
const MySpacePanel = lazy(() => import('./myspace/MySpacePanel').then(m => ({ default: m.MySpacePanel })));
const ProjectsPanel = lazy(() => import('./projects/ProjectsPanel').then(m => ({ default: m.default })));
const ProjectDetailPanel = lazy(() => import('./projects/ProjectDetailPanel').then(m => ({ default: m.default })));
const SettingsPage = lazy(() => import('./settings/SettingsModal').then(m => ({ default: m.default })));
const SitesPanel = lazy(() => import('./sites/SitesPanel').then(m => ({ default: m.SitesPanel })));
const panelLoading = <div className="jx-skeletonBlock" style={{ minHeight: 160 }} role="status" aria-label={t('加载中…')} />;
const { Header, Content } = Layout;
export function AppWorkspace({ state }: { state: ReturnType<typeof useAppController> }) {
  const { handleNewChat, handleNewProjectChat, deleteChat, toggleChatPinned, toggleChatFavorite, startRenameChat, commitRenameChat, exportChatRecord, handleSelectChat, handleSetPanel, siderCollapsed, setSiderCollapsed, handleSelectSearchResult, canvasFullscreen, canvasPanelWidth, canvasOpen, chatSurface, showChatHeader, openMobileSidebar, isEmptyChat, handleRightSidebarToggle, showHeader, title, hint, chat, chatProjectName, recommendBarVisible, recommendBannerText, handleCapabilityClick, setRecommendBarVisible, handleContentRef, panel, send, abort, activateQueuedMessage, discardQueuedMessage, continueLoop, createChatShare, handleFileSelect, removeFile, regenerate, editAndResendFollow, inputRef, fileInputRef, chatListRef, messagesEndRef, currentProjectId, setCatalogPanel, toolResultPanel, promptHubOpen, isCE, automationActiveGroup, rightSidebarView, detailModal, setDetailModal, refreshCatalog, cancelAndResumeBatch } = state;
  return (
    <Layout className="jx-appShell" style={{ height: '100%' }}>
      <Sidebar
        onNewChat={handleNewChat}
        onNewProjectChat={handleNewProjectChat}
        onDeleteChat={deleteChat}
        onTogglePinned={toggleChatPinned}
        onToggleFavorite={toggleChatFavorite}
        onStartRename={startRenameChat}
        onCommitRename={commitRenameChat}
        onExportChat={(id) => void exportChatRecord(id)}
        onSelectChat={handleSelectChat}
        onSetPanel={handleSetPanel}
      />
      {!siderCollapsed && (
        <button
          type="button"
          className="jx-mobileSidebarBackdrop"
          onClick={() => setSiderCollapsed(true)}
          aria-label={t('关闭侧边栏')}
        />
      )}

      {/* Global search modal: triggered by the search button / ⌘K / Ctrl+K */}
      <SearchModal
        onNewChat={handleNewChat}
        onSelectChat={handleSelectChat}
        onSelectSearchResult={handleSelectSearchResult}
      />

      <Layout className={`jx-appMainLayout${canvasFullscreen ? ' is-canvasFullscreen' : ''}`} style={{ overflow: 'hidden', background: 'var(--color-bg-base)', '--jx-canvas-preferred-width': canvasPanelWidth ? `${canvasPanelWidth}px` : undefined } as CSSProperties}>
        <div className={`jx-primaryPane${canvasOpen ? ' is-canvasOpen' : ''}${chatSurface ? ' is-chatSurface' : ''}`}>
          {!showChatHeader && (
            <header className="jx-mobileHeader">
              <button
                type="button"
                className="jx-mobileMenuBtn"
                onClick={openMobileSidebar}
                aria-label={t('打开侧边栏')}
              >
                <MenuOutlined />
              </button>
            </header>
          )}
          {chatSurface && !isEmptyChat && !canvasOpen && (
            <Tooltip title={t('展开右侧面板')} placement="bottomRight">
              <Button
                type="text"
                className="jx-rightSidebarToggle"
                icon={<InsertRowRightOutlined />}
                onClick={handleRightSidebarToggle}
                aria-label={t('展开右侧面板')}
                aria-pressed="false"
              />
            </Tooltip>
          )}
          {/* Non-chat panels: standard header */}
          {showHeader && (
            <Header className="jx-topbar" style={{ paddingInline: 20, display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12, minWidth: 0, flex: 1 }}>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <Typography.Title level={5} style={{ margin: 0, fontWeight: 900 }} ellipsis>{title}</Typography.Title>
                  <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 2 }} ellipsis>{hint}</Typography.Text>
                </div>
              </div>
            </Header>
          )}

          {/* Chat panel with messages: minimal header with title */}
          {showChatHeader && (
            <div className="jx-chatTopbar">
              <button
                type="button"
                className="jx-mobileMenuBtn"
                onClick={openMobileSidebar}
                aria-label={t('打开侧边栏')}
              >
                <MenuOutlined />
              </button>
              {chat?.projectId && (
                <span
                  className="jx-chatTopbarProject"
                  title={`${t('项目：')}${chatProjectName || t('项目')}`}
                  onClick={() => { void useProjectStore.getState().openProject(chat.projectId!); }}
                >
                  {chatProjectName || t('项目')}
                  <span className="jx-chatTopbarProjectSep">/</span>
                </span>
              )}
              <span className="jx-chatTopbarTitle">{chat?.title || t('对话')}</span>
              {chat?.agentName && (
                <Tag className="jx-headerTopicTag" color="blue">{chat.agentName}</Tag>
              )}
              {/* Follows the live composer mode, not the historical planChat marker: once the user
                closes plan mode the header must stop claiming the chat is still in it. */}
              {resolvePlanModeActive(chat) && (
                <Tag className="jx-headerTopicTag" color="blue">{t('计划模式')}</Tag>
              )}
              {chat?.businessTopic && (
                <Tag className="jx-headerTopicTag" color={TOPIC_TAG_COLORS[chat.businessTopic] || 'default'}>{chat.businessTopic}</Tag>
              )}
            </div>
          )}

          {/* Chat empty state: closable recommend banner (full-width); on close the height collapses so the content below moves up smoothly */}
          <CollapseHeight
            show={chatSurface && isEmptyChat && recommendBarVisible}
            motionKey="recommend-banner"
            duration={0.2}
            style={{ flex: 'none' }}
          >
            <div className="jx-recommendBanner">
              <span className="jx-recommendBanner-icon">💡</span>
              <span className="jx-recommendBanner-text">
                {recommendBannerText.trim() || t('推荐用法：优先使用知识库检索可提升可引用性与结果可靠性。')}
                <a className="jx-recommendBanner-link" onClick={() => handleCapabilityClick('knowledge')}>{t('前往知识库 >')}</a>
              </span>
              <button className="jx-recommendBanner-close" onClick={() => setRecommendBarVisible(false)} aria-label={t('关闭')}>
                <CloseOutlined style={{ fontSize: 16 }} />
              </button>
            </div>
          </CollapseHeight>


          <div className="jx-mainRow">
            <Content ref={handleContentRef} className={`jx-content${chatSurface ? ' jx-content--chatSurface' : ''}`}>
              {/* Unified panel-switch entrance (fade+rise, enter-only to stay responsive); key=panel:
              * switching chats within the chat panel does not replay it. One-way entrance
              * needs no motion — CSS primitives suffice. */}
              <div
                key={panel}
                className="jx-panel jx-anim-fadeInUp"
                data-panel={panel}
                style={{ '--fadeInUp-distance': '6px', animationDuration: '180ms' } as React.CSSProperties}
              >
                {chatSurface && (
                  <ChatArea
                    send={send}
                    abort={abort}
                    activateQueuedMessage={activateQueuedMessage}
                    discardQueuedMessage={discardQueuedMessage}
                    continueLoop={continueLoop}
                    exportChatRecord={exportChatRecord}
                    createChatShare={createChatShare}
                    handleFileSelect={handleFileSelect}
                    removeFile={removeFile}
                    regenerate={regenerate}
                    editAndResend={editAndResendFollow}
                    inputRef={inputRef}
                    fileInputRef={fileInputRef}
                    chatListRef={chatListRef}
                    messagesEndRef={messagesEndRef}
                  />
                )}
                <Suspense fallback={panelLoading}>
                {panel === 'ability_center' && <AbilityCenterPage />}
                {panel === 'docs' && <DocsPanel />}
                {panel === 'app_center' && <AppCenterPanel />}
                {panel === 'automation' && !chatSurface && <AutomationPanel />}
                {panel === 'sites' && <SitesPanel />}
                {panel === 'lab' && <LabPanel />}
                {panel === 'settings' && <SettingsPage />}
                {panel === 'my_space' && <MySpacePanel />}
                {panel === 'projects' && <ProjectsPanel onOpenProject={(pid) => { void useProjectStore.getState().openProject(pid); }} />}
                {panel === 'project_detail' && currentProjectId && (
                  <ProjectDetailPanel
                    projectId={currentProjectId}
                    onBack={() => setCatalogPanel('projects')}
                    handleFileSelect={handleFileSelect}
                    removeFile={removeFile}
                  />
                )}
                </Suspense>
              </div>
            </Content>

            <SlidePanel show={!!toolResultPanel && !promptHubOpen && !canvasOpen && chatSurface} panelKey="tool-result-panel" x={20} duration={0.22}>
              <ToolResultPanel />
            </SlidePanel>
            <SlidePanel show={!isCE && promptHubOpen && !canvasOpen && (chatSurface || panel === 'project_detail')} panelKey="prompt-hub">
              <PromptHubPanel />
            </SlidePanel>
            {/* Automation run timeline — persistent panel (not mutually exclusive with SlidePanels).
            * During exit store.activeGroup is already null; RunTimelinePanel falls back to a
            * snapshot internally to render the last frame. */}
            <SlidePanel show={!!automationActiveGroup && panel === 'automation' && chatSurface} panelKey="run-timeline" x={24} duration={0.24}>
              <Suspense fallback={panelLoading}><RunTimelinePanel /></Suspense>
            </SlidePanel>
          </div>
        </div>

        <SlidePanel
          show={canvasOpen}
          panelKey="canvas"
          className={`jx-canvasPanelSlot${rightSidebarView === 'file' ? '' : ' jx-rightSidebarSlot'}${canvasFullscreen ? ' is-fullscreen' : ''}`}
          x={30}
          duration={0.28}
        >
          <RightSidebarPanel />
        </SlidePanel>
      </Layout>

      {/* Global modals */}
      <Modal
        title={detailModal?.title}
        open={!!detailModal}
        onCancel={() => setDetailModal(null)}
        footer={<Button onClick={() => setDetailModal(null)}>{t('关闭')}</Button>}
        width={640}
        className="jx-detailModal"
        destroyOnHidden
      >
        {detailModal?.body}
      </Modal>

      <ImagePreview />
      <CreateKBModal onCreated={() => void refreshCatalog()} />
      <ReindexModal />
      <AuthExpiredModal />
      <BatchConfirmModal onCancelResume={cancelAndResumeBatch} />
    </Layout>
  );
}
