import { chatDraftKey, projectDraftKey, useComposerDraft, useComposerStore } from '../../stores/composerStore';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useChatStore, useUIStore, useCatalogStore, useAuthStore, usePluginStore, usePluginUiStore, useEditionStore } from '../../stores';
import { useProjectStore } from '../../stores/projectStore';
import { projectCreationTargets, useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { useAgentStore } from '../../stores/agentStore';
import { useModelCapabilitiesStore } from '../../stores/modelCapabilitiesStore';
import { createLocalProject, getProject } from '../../api';
import type { ProjectDetail } from '../../types';
import { resolveBatchModeActive, resolveWorkflowModeActive } from '../../utils/chatMode';
import { canInitializeProject } from '../../utils/projectCommands';
import type { ComposerOptions } from './composerTypes';

export function useComposerState({
  projectComposer, forceSendMode, activeMode, onEnterMode: onEnterModeProp, abort,
}: ComposerOptions) {
  const {
    sending: storeSending,
    planMode, loopMode, setLoopMode, currentChat, enterChatMode, exitChatMode,
    currentChatId, bindChatProject, unbindChatProject,
    queuedMessages, updateQueuedMessage, activeRuns,
  } = useChatStore();
  // Autonomous-loop capability bit (enabled by default): without permission the "autonomous loop" toggle is hidden
  const loopCapEnabled = useAuthStore((s) => s.authUser?.can_run_autonomous_loop);
  // Lab permission (undefined defaults to enabled): the autonomous loop is an experimental ability, only shown in lab users' chats
  const labEnabled = useAuthStore((s) => s.authUser?.lab_enabled);
  // Which apps are open to the current user (same allowed_apps gate as the "App Center")
  const allowedApps = useAuthStore((s) => s.authUser?.allowed_apps ?? null);
  const isAppAllowed = (id: string) => !Array.isArray(allowedApps) || allowedApps.includes(id);
  const planModeAllowed = !Array.isArray(allowedApps) || allowedApps.includes('plan_mode');
  const batchRunnerAllowed = !Array.isArray(allowedApps) || allowedApps.includes('batch_runner');
  // Skill list (for the skills submenu of the "+" menu)
  const skills = useCatalogStore((s) => s.catalog.skills);
  // Enabled MCP servers are user-facing "connectors" in the composer.
  const connectors = useCatalogStore((s) => s.catalog.mcp);
  // Project list (for the toolbar "Project" selector dropdown)
  const projects = useProjectStore((s) => s.list);
  const fetchProjects = useProjectStore((s) => s.fetchProjects);
  const detailProject = useProjectStore((s) => s.currentProject);
  const setProjectCreateModalOpen = useProjectStore((s) => s.setCreateModalOpen);
  // Sub-agent list (for the "@sub-agent" submenu of the "+" menu)
  const agents = useAgentStore((s) => s.agents);
  const fetchAgents = useAgentStore((s) => s.fetchAgents);
  // Installed plugins (for the "Plugins" submenu of the "+" menu + the / slash popup).
  // Uses the shared store: the capability center forces a refresh after install/uninstall,
  // so this syncs immediately (avoids fetching only on mount, which would hide newly installed plugins).
  const installedPlugins = usePluginStore((s) => s.installed);
  useEffect(() => {
    void usePluginStore.getState().fetchInstalled();
    // 插件贡献的工具卡片/画布声明也在这里首次拉取：对话面板是它们的主要出场位置。
    void usePluginUiStore.getState().fetchContributions();
  }, []);
  const sending = forceSendMode ? false : storeSending;
  const draftKey = projectComposer && detailProject
    ? projectDraftKey(detailProject.project_id) : chatDraftKey(currentChatId);
  const visibleKey = useComposerStore(s => s.activeKey);
  useLayoutEffect(() => {
    if (visibleKey !== draftKey) useComposerStore.getState().activate(draftKey);
  }, [draftKey, visibleKey]);
  const {
    input, setInput, quotedFollowUp, setQuotedFollowUp,
    activeSkill, setActiveSkill, activePlugin, setActivePlugin,
    activeConnector, setActiveConnector, activeMention, setActiveMention,
    activeCommand, setActiveCommand, referencedChats, addReferencedChat,
    removeReferencedChat, uploadedFiles, uploadingFiles, importedSpaceFiles,
  } = useComposerDraft(draftKey);
  const { promptHubOpen, setPromptHubOpen } = useUIStore();
  const isCE = useEditionStore((s) => s.edition === 'ce');
  const _currentChat = currentChat();
  // Batch mode as the composer currently runs it — the persistent batchChat marker is only its
  // default, so a chat the user took out of batch mode no longer counts as one here.
  const batchModeOn = resolveBatchModeActive(_currentChat);
  const workflowModeOn = resolveWorkflowModeActive(_currentChat);
  const isSiteChat = !!_currentChat?.siteChat;
  // Whether the "autonomous loop" entry is shown: normal chat (not plan/batch/project page)
  // + has the loop capability bit + has lab permission. When eligible it no longer occupies
  // the toolbar but is tucked into the "+" attachment menu, visible to lab users only.
  const showLoopEntry =
    !planMode && !batchModeOn && !projectComposer && loopCapEnabled !== false && labEnabled !== false;
  // 当前部署能否读图：主模型原生多模态，或后台配了「图像理解（视觉桥）」角色。
  // 未加载完成时按 true 处理，避免首屏闪出一句「不识图」又立刻收回。
  const canReadImage = useModelCapabilitiesStore(
    (s) => !s.loaded || s.capabilities.can_read_image !== false,
  );
  const imageInputRef = useRef<HTMLInputElement | null>(null);
  const [mySpaceImportOpen, setMySpaceImportOpen] = useState(false);
  // 工具条各下拉的展开态：只用于让 chip 亮起 + 箭头翻转，让"按钮/浮层"读起来是一体的
  const [projectOpen, setProjectOpen] = useState(false);
  const [attachOpen, setAttachOpen] = useState(false);
  // Module C: no My Space in local mode → hide "从我的空间导入".
  const activeLocalMode = useDeploymentModeStore((s) => s.activeLocal);
  const provisionMode = useDeploymentModeStore((s) => s.provisionMode);
  const isDesktopShell = useDeploymentModeStore((s) => s.isDesktop);
  const {
    cloud: canCreateCloudProject,
    local: canCreateLocalProject,
  } = projectCreationTargets(isDesktopShell, provisionMode);
  // 混合架构：双模式=云端身份 + 本机执行面，本地项目能力在 dual 下同样可用。
  const localCapable = canCreateLocalProject;
  // 项目下拉里的「新建本地项目」：跳壳的文件夹选择器（/__desktop/pick-local-folder），
  // 壳选完把路径以 hugagent:local-folder 事件回抛到页面；这里建项目、刷新列表并
  // 把当前对话直接绑定到新项目上（项目页 composer 不注册，避免双实例重复建）。
  useEffect(() => {
    if (projectComposer || !isDesktopShell || !localCapable) return;
    const onFolder = (e: Event) => {
      const path = (e as CustomEvent<string>).detail;
      if (!path) return;
      const name = path.split(/[/\\]/).filter(Boolean).pop() || '本地项目';
      createLocalProject({ name, local_path: path })
        .then((proj) => {
          void useProjectStore.getState().fetchProjects();
          const { currentChatId: chatId, bindChatProject: bind } = useChatStore.getState();
          if (chatId) bind(chatId, proj.project_id, proj.name);
        })
        .catch((err) => {
          alert('新建本地项目失败：' + (err?.message || err));
        });
    };
    window.addEventListener('hugagent:local-folder', onFolder as EventListener);
    return () => window.removeEventListener('hugagent:local-folder', onFolder as EventListener);
  }, [projectComposer, isDesktopShell, localCapable]);

  const commandProjectId = projectComposer ? detailProject?.project_id : _currentChat?.projectId;
  const [loadedCommandProject, setLoadedCommandProject] = useState<ProjectDetail | null>(null);
  const listedCommandProject = detailProject?.project_id === commandProjectId
    ? detailProject : projects.find((p) => p.project_id === commandProjectId);
  // Existing chats can refer to projects outside the paginated selector list.
  useEffect(() => {
    if (!commandProjectId || listedCommandProject) return;
    let cancelled = false;
    void getProject(commandProjectId).then((project) => {
      if (!cancelled) setLoadedCommandProject(project);
    }).catch(() => { if (!cancelled) setLoadedCommandProject(null); });
    return () => { cancelled = true; };
  }, [commandProjectId, listedCommandProject]);
  const commandProject = listedCommandProject
    ?? (loadedCommandProject?.project_id === commandProjectId ? loadedCommandProject : null);
  const canInitProject = canInitializeProject({
    projectId: commandProjectId,
    permission: commandProject?.permission,
    busy: sending,
    hasCapability: !!(activeSkill || activePlugin || activeConnector || activeMention
      || (!projectComposer && _currentChat?.agentId)),
    specialMode: !!(projectComposer ? activeMode : planMode || batchModeOn || workflowModeOn || loopMode),
  });

  // ── Project binding (toolbar "Project" selector dropdown, to the right of the Prompt Hub) ──
  const boundProjectId = _currentChat?.projectId;
  const boundProjectName =
    _currentChat?.projectName ||
    projects.find((p) => p.project_id === boundProjectId)?.name ||
    '';

  function onPickProject(projectId: string, projectName: string) {
    bindChatProject(currentChatId, projectId, projectName);
  }

  /** Whether the composer currently runs in this mode (main composer: live composer state;
   *  project composer: the pending selection passed in via activeMode). */
  function isModeOn(mode: 'plan' | 'batch' | 'workflow') {
    if (projectComposer) return activeMode === mode;
    if (mode === 'plan') return planMode;
    if (mode === 'workflow') return workflowModeOn;
    return batchModeOn;
  }

  /** Enter plan / batch-execution mode from the "+" menu. The project page customizes this via
   *  the onEnterMode prop (defer chat creation until send, no navigation); the default switches
   *  the current chat to that mode in place — no new chat, no navigation (avoids bouncing the
   *  whole chat back to the home page). */
  function onEnterMode(mode: 'plan' | 'batch' | 'workflow') {
    if (onEnterModeProp) {
      onEnterModeProp(mode);
      return;
    }
    enterChatMode(mode, { inPlace: true });
  }

  /** Close a running mode from the ✕ on its composer chip — the single, always-visible way out.
   *  On the project page the pending selection is owned by the parent, so hand the toggle back
   *  to it (onEnterModeProp flips the already-selected mode off). */
  function onCloseMode(mode: 'plan' | 'batch' | 'workflow') {
    // 关掉计划模式的同时得真的把正在跑的计划停下来。原来这里只翻了个
    // planModeActive 标志位：后端的计划和 run 继续跑，卡片也一直挂在「执行中」，
    // 用户以为已经关掉了（问题 31）。abort() 会取消 run、取消计划、并把卡片落成已中断。
    if (mode === 'plan' && sending) {
      abort?.();
    }
    if (onEnterModeProp) {
      onEnterModeProp(mode);
      return;
    }
    exitChatMode(mode);
  }


  return {
    draftKey,
    input, setInput, quotedFollowUp, setQuotedFollowUp, activeSkill, setActiveSkill,
    activePlugin, setActivePlugin, activeConnector, setActiveConnector, activeMention,
    setActiveMention, activeCommand, setActiveCommand, planMode, loopMode, setLoopMode,
    currentChatId, bindChatProject, unbindChatProject, queuedMessages, updateQueuedMessage,
    activeRuns, referencedChats, addReferencedChat, removeReferencedChat,
    isAppAllowed, planModeAllowed, batchRunnerAllowed, skills, connectors, projects,
    fetchProjects, setProjectCreateModalOpen, agents, fetchAgents, installedPlugins,
    sending, uploadedFiles, uploadingFiles, importedSpaceFiles, promptHubOpen, setPromptHubOpen,
    isCE, _currentChat, batchModeOn, workflowModeOn, isSiteChat, showLoopEntry, canReadImage,
    imageInputRef, mySpaceImportOpen, setMySpaceImportOpen, projectOpen, setProjectOpen,
    attachOpen, setAttachOpen, activeLocalMode, isDesktopShell, canCreateCloudProject,
    canCreateLocalProject, canInitProject, boundProjectId, boundProjectName,
    onPickProject, isModeOn, onEnterMode, onCloseMode,
  };
}

export type ComposerState = ReturnType<typeof useComposerState>;
