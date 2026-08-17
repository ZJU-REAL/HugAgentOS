import { create } from 'zustand';
import { getSidebarChatOrder, saveSidebarChatOrder } from '../api';
import { loadJsonPref, saveJsonPref, userScopedKey } from '../storage';
import { mergeGroupOrder, reorderGroupSequence } from '../utils/sidebarOrder';

/**
 * 侧边栏对话列表的「手动顺序」真源。
 *
 * 默认排序是派生的（置顶优先 + updatedAt 倒序），用户一旦手动拖过，就要有一份
 * 显式顺序压住它——否则下一条消息把 updatedAt 一顶，手动摆的位置立刻被冲掉。
 *
 * 数据形态：一维 chat_id 序列。跨分组穿插不影响正确性，因为比较只发生在同一
 * 分组内部（历史/自动化/某个项目），序列只用来取相对下标。
 * 不在序列里的会话＝没被手动排过，按 updatedAt 排在该组最前（新会话自然冒头）。
 *
 * 持久化两层：localStorage（按账号隔离，秒开、离线可用）+ 服务端 users_shadow.metadata
 * （换设备跟随账号）。服务端一旦读到就是真源。
 */

const SIDEBAR_ORDER_KEY = 'hugagent_ui_sidebar_order_v1';
/** 与后端 SIDEBAR_ORDER_MAX 对齐，避免本地攒出后端会截断的长尾。 */
const MAX_ORDER_LEN = 500;

const localKey = (userId: string | null) => userScopedKey(SIDEBAR_ORDER_KEY, userId);

function readLocal(userId: string | null): string[] {
  const key = localKey(userId);
  if (!key) return [];
  const raw = loadJsonPref<string[]>(key, []);
  return Array.isArray(raw) ? raw.filter((id) => typeof id === 'string') : [];
}

function writeLocal(userId: string | null, order: string[]) {
  const key = localKey(userId);
  if (key) saveJsonPref(key, order);
}

export interface SidebarOrderState {
  /** 手动顺序序列；空数组＝从未手动排过，走默认排序 */
  order: string[];
  /** 当前已 hydrate 的账号，切账号时用于判断是否需要重读 */
  userId: string | null;
  /** 正在拖拽的会话 id（拖拽期间关掉列表布局动画，避免与拖影打架） */
  draggingId: string | null;
  /** 当前落点提示：拖到哪个会话的上边/下边 */
  dropTarget: { id: string; place: 'before' | 'after' } | null;

  /** 切换账号：先用本地顺序秒开，再异步拉服务端顺序覆盖 */
  hydrateForUser: (userId: string) => void;
  setDragging: (id: string | null) => void;
  setDropTarget: (target: { id: string; place: 'before' | 'after' } | null) => void;
  /** 在同一分组内把 draggedId 挪到 targetId 的前/后，并落库 */
  reorderWithinGroup: (
    groupIds: string[],
    draggedId: string,
    targetId: string,
    place: 'before' | 'after',
  ) => void;
  /** 清空手动顺序 → 回到「置顶 + 最近更新」默认排序 */
  resetOrder: () => void;
}

export const useSidebarOrderStore = create<SidebarOrderState>((set, get) => ({
  order: [],
  userId: null,
  draggingId: null,
  dropTarget: null,

  hydrateForUser: (userId) => {
    if (get().userId === userId) return;
    set({ order: readLocal(userId), userId, draggingId: null, dropTarget: null });
    void (async () => {
      try {
        const remote = await getSidebarChatOrder();
        // 账号在等待期间又切走了 → 丢弃这次响应
        if (useSidebarOrderStore.getState().userId !== userId) return;
        if (remote.length > 0) {
          set({ order: remote });
          writeLocal(userId, remote);
          return;
        }
        // 服务端还没有顺序但本地有（老版本本地排过 / 首次上线）→ 把本地补写上去
        const local = useSidebarOrderStore.getState().order;
        if (local.length > 0) await saveSidebarChatOrder(local);
      } catch {
        /* 服务端不可用：本地顺序照常生效，下次拖拽再补同步 */
      }
    })();
  },

  setDragging: (id) => set({ draggingId: id, ...(id ? {} : { dropTarget: null }) }),
  setDropTarget: (target) => set({ dropTarget: target }),

  reorderWithinGroup: (groupIds, draggedId, targetId, place) => {
    // 以该组「当前可见顺序」为底稿重排：第一次拖拽就把整组冻结成显式顺序，
    // 之后 updatedAt 再变也不会把别的会话顶上来。
    const nextGroupSeq = reorderGroupSequence(groupIds, draggedId, targetId, place);
    if (!nextGroupSeq) {
      set({ draggingId: null, dropTarget: null });
      return;
    }
    const nextOrder = mergeGroupOrder(get().order, groupIds, nextGroupSeq, MAX_ORDER_LEN);

    const { userId } = get();
    set({ order: nextOrder, draggingId: null, dropTarget: null });
    writeLocal(userId, nextOrder);
    void saveSidebarChatOrder(nextOrder).catch(() => {
      /* 落库失败不回滚本地：顺序是纯偏好，本地已生效，下次拖拽会整表重传 */
    });
  },

  resetOrder: () => {
    const { userId } = get();
    set({ order: [], draggingId: null, dropTarget: null });
    writeLocal(userId, []);
    void saveSidebarChatOrder([]).catch(() => { /* 同上，best-effort */ });
  },
}));
