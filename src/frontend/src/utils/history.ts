import type { ChatItem, ChatStore, AutomationTask } from '../types';
import type { HistoryTimeFilter } from '../stores/uiStore';
import { t } from '../i18n';

interface AutomationSidebarPref { pinned?: boolean; favorite?: boolean }

/**
 * Combines regular chats + automation task virtual items into the ChatItem array shared
 * by the Sidebar / SearchModal.
 * The logic in both consumers must stay exactly in sync, otherwise you get the weird UX
 * of "the sidebar has this item but the search modal doesn't".
 */
export function buildSidebarChatItems(
  store: ChatStore,
  sidebarTasks: AutomationTask[],
  sidebarPrefs: Record<string, AutomationSidebarPref>,
): ChatItem[] {
  const ids = (store.order || []).filter((id) => store.chats[id]);
  const normalChats = ids
    .map((id) => store.chats[id])
    .filter((item) => !isAutomationHistoryChat(item));

  const automationItems: ChatItem[] = sidebarTasks.map((task) => ({
    id: `automation:${task.task_id}`,
    title: task.name || task.prompt?.slice(0, 30) || t('定时任务'),
    createdAt: new Date(task.created_at).getTime(),
    updatedAt: task.last_run_at ? new Date(task.last_run_at).getTime() : new Date(task.updated_at).getTime(),
    messages: [],
    pinned: !!sidebarPrefs[task.task_id]?.pinned,
    favorite: !!sidebarPrefs[task.task_id]?.favorite,
    automationTaskId: task.task_id,
    automationRun: true,
  }));

  return [...normalChats, ...automationItems];
}

export type HistoryGroupKey = 'today' | 'yesterday' | 'week' | 'month' | 'older';

export function inferBusinessTopic(text: string): string {
  const q = text.toLowerCase();
  if (/政策|条例|条款|法规|依据|适用范围/.test(q)) return '政策解读';
  if (/办理|流程|审批|申报|事项|材料清单/.test(q)) return '事项办理';
  if (/对照|差异|比对|对比/.test(q)) return '材料比对';
  if (/知识库|检索|文档|引用|出处/.test(q)) return '知识检索';
  if (/统计|图表|数据|趋势|分析|报表/.test(q)) return '数据分析';
  return '综合咨询';
}

export function matchesTimeFilter(ts: number, filter: HistoryTimeFilter): boolean {
  if (filter === 'all') return true;
  const now = new Date();
  if (filter === 'today') {
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    return ts >= today;
  }
  const day = 24 * 60 * 60 * 1000;
  if (filter === '7d') return now.getTime() - ts <= 7 * day;
  if (filter === '30d') return now.getTime() - ts <= 30 * day;
  return true;
}

export function getHistoryDayDiff(ts: number): number {
  const now = new Date();
  const nowDayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const target = new Date(ts);
  const targetDayStart = new Date(target.getFullYear(), target.getMonth(), target.getDate()).getTime();
  const dayDiff = Math.floor((nowDayStart - targetDayStart) / (24 * 60 * 60 * 1000));
  return Math.max(0, dayDiff);
}

export function getHistoryGroupKey(ts: number): HistoryGroupKey {
  const dayDiff = getHistoryDayDiff(ts);
  if (dayDiff === 0) return 'today';
  if (dayDiff === 1) return 'yesterday';
  if (dayDiff <= 7) return 'week';
  if (dayDiff <= 30) return 'month';
  return 'older';
}

export function looksLikeAutomationTitle(title?: string): boolean {
  if (!title) return false;
  return title.trim().startsWith('[自动化]');
}

export function isAutomationHistoryChat(
  item?: Pick<ChatItem, 'title' | 'automationRun' | 'automationTaskId' | 'planChat' | 'agentId'> | null,
): boolean {
  if (!item) return false;
  if (item.automationRun === true || !!item.automationTaskId) return true;
  if (item.planChat || item.agentId) return false;
  return looksLikeAutomationTitle(item.title);
}

/**
 * 「编辑站点」该落到哪段会话上。
 *
 * 站点是从某段对话里建出来的，「编辑」理应回到那段对话继续改。只认"已经聊过的"
 * 会话会让连点几次「编辑」造出一串同名空会话挂在同一个源码工程下 —— 它们只存在
 * 于本地（后端没有对应会话），要刷新才消失，多开窗口时尤其扎眼。所以：聊过的
 * 优先，没聊过的空会话也复用，都没有才返回 undefined 由调用方新建。
 */
export function pickSiteEditChat<T extends Pick<ChatItem, 'siteChat' | 'projectId' | 'updatedAt' | 'messages'>>(
  chats: T[],
  projectId: string,
): T | undefined {
  const candidates = chats
    .filter((c) => c.siteChat && c.projectId === projectId)
    .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
  return candidates.find((c) => (c.messages?.length || 0) > 0) || candidates[0];
}
