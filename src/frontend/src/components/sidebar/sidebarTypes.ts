import type { ChatItem, PanelKey } from '../../types';
export interface SidebarProps {
  onNewChat: () => void;
  onNewProjectChat: (projectId: string, projectName: string) => void;
  onDeleteChat: (id: string) => void;
  onTogglePinned: (id: string) => void;
  onToggleFavorite: (id: string) => void;
  onStartRename: (item: ChatItem) => void;
  onCommitRename: (id: string) => void;
  onExportChat: (id: string) => void;
  onSelectChat: (id: string) => void;
  /** `sub` 是面板内的二级页（能力中心的类别），必须随这一次进入一起给出。 */
  onSetPanel: (p: PanelKey, sub?: string) => void;
}
