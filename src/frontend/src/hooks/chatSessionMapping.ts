import { authFetch, isLocalChat, registerLocalChat, toPlanProgress, LOCAL_TARGET_HEADER } from '../api';
import { useChatStore } from '../stores';
import type { ChatItem } from '../types';

/** 服务端会话行 → 侧边栏条目。`prior` 是刷新前本地已有的同一会话（保留手动改名、模式开关等本地态）。 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function sessionToChatItem(s: any, prior?: ChatItem): ChatItem {
  const id: string = s.chat_id;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const meta = (s.metadata || {}) as any;
  // 手动重命名保护：后端已带 title_manually_set 直接用；本地改过名但还没
  // 同步到后端（流式期间改名后刷新）→ 保留本地标题，后续流结束时自动补同步
  const inherited = !!meta.fork?.source_chat_id;
  const localManual = prior?.titleManuallySet === true;
  const backendManual = meta.title_manually_set === true;
  const preservedTitle = !backendManual && localManual && prior?.title
    ? prior.title
    : (s.title || '新对话');
  return {
    id,
    title: preservedTitle,
    ...(backendManual || localManual ? { titleManuallySet: true } : {}),
    createdAt: s.created_at ? new Date(s.created_at).getTime() : Date.now(),
    updatedAt: s.updated_at ? new Date(s.updated_at).getTime() : Date.now(),
    messages: [],
    favorite: !!s.favorite,
    pinned: !!s.pinned,
    businessTopic: meta.businessTopic || '综合咨询',
    agentId: meta.agent_id || undefined,
    agentName: meta.agent_name || undefined,
    planChat: meta.plan_chat === true ? true : undefined,
    ...(typeof prior?.planModeActive === 'boolean'
      ? { planModeActive: prior.planModeActive }
      : {}),
    batchChat: meta.batch_chat === true ? true : undefined,
    ...(typeof prior?.batchModeActive === 'boolean'
      ? { batchModeActive: prior.batchModeActive }
      : {}),
    workflowChat: meta.workflow_chat === true ? true : undefined,
    ...(typeof prior?.workflowModeActive === 'boolean'
      ? { workflowModeActive: prior.workflowModeActive }
      : {}),
    automationTaskId: typeof meta.automation_task_id === 'string' ? meta.automation_task_id : undefined,
    automationRun: meta.automation_run === true ? true : undefined,
    planProgress: toPlanProgress(meta.plan_progress),
    // When the backend session hasn't bound project_id (e.g. bound locally via the input-box dropdown, not yet persisted with a message),
    // keep the locally bound projectId/projectName — otherwise the session would fall back to the default project after refresh. The next send
    // carries project_id and self-heals into the DB.
    projectId: (typeof s.project_id === 'string' && s.project_id)
      ? s.project_id
      : (prior?.projectId || undefined),
    projectName: prior?.projectName || undefined,
    modeSlug: typeof meta.mode_slug === 'string' ? meta.mode_slug : prior?.modeSlug,
    thinkingEffort: ['turbo', 'fast', 'medium', 'high', 'max'].includes(meta.thinking_effort)
      ? meta.thinking_effort : prior?.thinkingEffort,
    ...(inherited ? {
      planModeActive: prior?.planModeActive ?? false,
      batchModeActive: prior?.batchModeActive ?? false,
      workflowModeActive: prior?.workflowModeActive ?? false,
    } : {}),
    ...(prior?.runTarget ? { runTarget: prior.runTarget } : {}),
  };
}

/** 侧边栏条目是否属于本机执行面。`isLocalChat` 还覆盖"挂在本地项目下"的会话——
 *  刷新后内存里的本机登记表是空的，只剩 runTarget 留在本地存储里，两者都要认。 */
export function isLocalSidebarChat(id: string, chat: ChatItem): boolean {
  return isLocalChat(id) || chat.runTarget === 'local';
}

/** 把本机执行面的会话并进侧边栏，并登记 chat→本机路由。
 *
 *  本机会话不归云端管，压根不会出现在云端列表里，所以侧边栏必须两面都问——
 *  与 api.ts 的 listActiveChatRuns / listPendingUserQuestions 是同一套做法。
 *  拉不到就抛错交给调用方：本机执行面就绪后会再并一次。 */
export async function mergeLocalSessions(apiUrl: string, isCancelled: () => boolean): Promise<void> {
  const r = await authFetch(`${apiUrl}/v1/chats?page_size=100&exclude_automation=true`, {
    headers: { [LOCAL_TARGET_HEADER]: 'local' },
  });
  if (!r.ok) throw new Error(`local sessions: HTTP ${r.status}`);
  const payload = await r.json();
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const items: any[] = payload?.data?.items || [];
  if (isCancelled()) return;
  const st = useChatStore.getState();
  const snapshot = st.store;
  const currentChat = st.currentChatId;
  let currentIsLocal = false;
  st.updateStore((prev) => {
    const chats = { ...prev.chats };
    const order = [...prev.order];
    for (const s of items) {
      const id: string = s.chat_id;
      registerLocalChat(id);
      st.addBackendSessionId(id);
      if (!chats[id]) {
        chats[id] = sessionToChatItem(s, snapshot.chats[id]);
        order.push(id);
      }
      chats[id] = { ...chats[id], runTarget: 'local' };
      if (id === currentChat) currentIsLocal = true;
    }
    return { ...prev, chats, order };
  });
  // 当前打开的正是本机会话时，重新触发它的历史加载。
  if (currentIsLocal) st.bumpSessionLoadEpoch();
}
