import { ChatSlot } from './customization/ChatCustomization';
import { lazy } from 'react';
import type { useAppController } from './hooks/useAppController';
import { useProjectStore } from './stores/projectStore';
import { ChatArea } from './components/chat';
export interface WorkspaceContext {
  state: ReturnType<typeof useAppController>;
  paneHidden: boolean;
}

const ProjectsPanel = lazy(() => import('./components/projects/ProjectsPanel'));
export function ConversationPage({ state, paneHidden }: WorkspaceContext) {
  return <ChatSlot name="conversation"><ChatArea
    isPaneHidden={paneHidden}
    send={state.send}
    abort={state.abort}
    activateQueuedMessage={state.activateQueuedMessage}
    discardQueuedMessage={state.discardQueuedMessage}
    continueLoop={state.continueLoop}
    exportChatRecord={state.exportChatRecord}
    createChatShare={state.createChatShare}
    handleFileSelect={state.handleFileSelect}
    removeFile={state.removeFile}
    regenerate={state.regenerate}
    editAndResend={state.editAndResendFollow}
    inputRef={state.inputRef}
    fileInputRef={state.fileInputRef}
    onViewportMount={state.handleContentRef}
    chatListRef={state.chatListRef}
    messagesEndRef={state.messagesEndRef}
  /></ChatSlot>;
}

export function ProjectListPage() {
  return <ProjectsPanel onOpenProject={pid => { void useProjectStore.getState().openProject(pid); }} />;
}
