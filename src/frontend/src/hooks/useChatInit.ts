import { useEffect, useRef } from 'react';
import { t } from '../i18n';
import { authFetch, checkSession, listActiveBatchPlans, getBatchPlan, chatTargetHeaders, isHybridDual, registerLocalChat, toPlanProgress, LOCAL_TARGET_HEADER } from '../api';
import { nowId, saveCatalog } from '../storage';
import { buildHistorySegments } from '../utils/segments';
import { attachArtifactsToToolCalls } from '../utils/fileParser';
import { isAutomationHistoryChat } from '../utils/history';
import { markResolvedPlanPreviews } from '../utils/planHistory';
import { stripMcpToolPrefix } from '../utils/constants';
import { parseContextCompactionState, parseContextUsageSnapshot } from '../utils/contextUsage';
import { shouldRestorePlanModeFromHistory } from '../utils/chatMode';
import { LOGIN_LANDING_KEY, useAuthStore, useSettingsStore, useUIStore, useChatStore, useCatalogStore, useAutomationChatStore, useBatchStore, useSidebarOrderStore } from '../stores';
import type { Catalog, ChatItem, ChatMessage, CitationItem, ContextCompactionState, ContextUsageSnapshot, EvolutionSummary, OntologyGovernanceSummary, ThinkingBlock, ToolCall, UpdateEntry, BatchPlanMeta, BatchSourceType, BatchItemResult } from '../types';

const effectiveApiUrl = (import.meta.env.VITE_API_BASE_URL as string || '').trim() || '/api';

// Chats with a message fetch currently in flight. Separate from
// `loadedMsgIds` on purpose: the store mark means "successfully loaded"
// (drives the skeleton-vs-empty-state UI), while this set is only the
// re-entrancy lock so concurrent effect runs / the startup preload don't
// double-fetch the same chat. Module-level is fine — it must survive
// re-renders but reset on page refresh, exactly like the store marks.
const inflightMsgLoads = new Set<string>();

// Per-chat failed message-load attempts. A non-2xx / network failure used to
// leave the chat stuck on the skeleton until the user switched away and back;
// now we self-retry a few times with backoff (bumpSessionLoadEpoch re-fires
// the lazy-load effect), then give up until the next manual visit.
const msgLoadRetryCounts = new Map<string, number>();
const MSG_LOAD_MAX_RETRIES = 3;

// Convert a backend message item (from GET /v1/chats/{id}/messages) into a
// frontend ChatMessage. Pure — used by both the preload path (during initial
// session fetch) and the lazy-load path (when switching into a never-loaded
// chat). Adding new fields persisted in metadata? Add them here, both paths
// pick it up automatically.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function parseHistoryMessage(m: any): ChatMessage {
  const allToolCalls: ToolCall[] | undefined = Array.isArray(m.tool_calls) && m.tool_calls.length > 0
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ? m.tool_calls.map((tc: any) => ({
        id: tc.tool_id ?? tc.id,
        name: stripMcpToolPrefix(tc.tool_name ?? tc.name ?? t('工具调用')),
        // in old history, tool_display_name may be the raw mcp__ name echoed back by the backend — discard it,
        // and let the TOOL_NAME_OVERRIDES / toolDisplayNames lookup chain take over by bare name.
        displayName: ((d) => (typeof d === 'string' && d.startsWith('mcp__') ? undefined : d))(
          tc.tool_display_name ?? tc.displayName,
        ),
        input: tc.tool_args ?? tc.arguments ?? tc.input,
        output: tc.result ?? tc.output,
        status: (tc.status === 'error'
          ? 'error'
          : tc.status === 'interrupted'
            ? 'interrupted'
            : 'success') as 'success' | 'error' | 'interrupted',
        timestamp: tc.timestamp,
        // sub-agent internal process (thinking + tool calls) — replayed from the DB after refresh
        ...(Array.isArray(tc.sub_steps ?? tc.subSteps)
          ? { subSteps: (tc.sub_steps ?? tc.subSteps) }
          : {}),
        ...(tc.subagent_name ?? tc.subagentName
          ? { subagentName: tc.subagent_name ?? tc.subagentName }
          : {}),
        ...(typeof tc.scope === 'string' ? { scope: tc.scope } : {}),
        ...(typeof (tc.content_offset ?? tc.contentOffset) === 'number'
          ? { contentOffset: (tc.content_offset ?? tc.contentOffset) }
          : {}),
      }))
    : undefined;
  const revisionToolCalls = allToolCalls?.filter((tool) => tool.scope === 'ontology_revision') ?? [];
  const baseToolCalls = allToolCalls?.filter((tool) => tool.scope !== 'ontology_revision');
  const metadataArtifacts = Array.isArray(m.metadata?.artifacts) ? m.metadata.artifacts : [];
  const toolCalls = attachArtifactsToToolCalls(
    baseToolCalls,
    metadataArtifacts,
    m.created_at ? new Date(m.created_at).getTime() : Date.now(),
  );

  const rawContent = String(m.content || '');
  // 思考单独存一列（新消息）；老消息该字段为空，思考仍内联在 content 里。
  const storedThinking = Array.isArray(m.thinking)
    ? (m.thinking as ThinkingBlock[]).filter((b) => b && typeof b.content === 'string')
    : undefined;
  let { segments, cleanContent } = m.role === 'assistant'
    ? buildHistorySegments(rawContent, toolCalls, storedThinking)
    : { segments: undefined, cleanContent: rawContent };

  // Reconstruct plan segment from saved plan_snapshot metadata
  const planSnapshot = m.metadata?.plan_snapshot;
  if (m.role === 'assistant' && planSnapshot && typeof planSnapshot === 'object') {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const snap = planSnapshot as any;
    const planSeg = {
      type: 'plan' as const,
      planData: {
        mode: (snap.mode || 'complete') as 'preview' | 'executing' | 'complete',
        planId: m.metadata?.plan_id ? String(m.metadata.plan_id) : undefined,
        title: String(snap.title || ''),
        description: snap.description ? String(snap.description) : undefined,
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        steps: Array.isArray(snap.steps) ? snap.steps.map((s: any) => ({
          step_order: Number(s.step_order ?? 0),
          title: String(s.title || ''),
          description: s.description ? String(s.description) : undefined,
          expected_tools: Array.isArray(s.expected_tools) ? s.expected_tools : [],
          expected_skills: Array.isArray(s.expected_skills) ? s.expected_skills : [],
          expected_agents: Array.isArray(s.expected_agents) ? s.expected_agents : [],
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          status: s.status as any,
          summary: s.summary ? String(s.summary) : undefined,
          text: s.ai_output ? String(s.ai_output) : undefined,
        })) : [],
        completedSteps: snap.completed_steps != null ? Number(snap.completed_steps) : undefined,
        totalSteps: snap.total_steps != null ? Number(snap.total_steps) : undefined,
        resultText: snap.result_text ? String(snap.result_text) : undefined,
        agentNameMap: snap.agent_name_map || undefined,
        // 中断状态要跟着历史一起回来：不带这一位的话，用户停掉的那一轮在下次
        // 拉历史后又渲染成「执行中」的转圈，看起来像自己又跑起来了。
        ...(snap.cancelled === true ? { cancelled: true } : {}),
      },
    };
    // Place plan segment first; add tool segments from saved tool_calls; then text.
    // In preview/executing mode, suppress the auto-generated "已生成执行计划：…"
    // text the backend stores as message content — the plan card already shows it.
    const toolSegs: typeof segments = toolCalls
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      ? toolCalls.map((_tc: any, idx: number) => ({ type: 'tool' as const, toolIndex: idx }))
      : [];
    const textSegments = snap.mode === 'complete' && snap.result_text
      ? [{ type: 'text' as const, content: String(snap.result_text) }]
      : [];
    segments = [planSeg, ...toolSegs, ...textSegments];
    cleanContent = snap.mode === 'complete' && snap.result_text
      ? String(snap.result_text)
      : '';
  }

  const rawAttachments = m.role === 'user' && Array.isArray(m.metadata?.attachments)
    ? m.metadata.attachments as Array<{ name: string; mime_type?: string; file_id?: string; download_url?: string }>
    : undefined;
  // Ensure download_url is populated from file_id when missing
  const histAttachments = rawAttachments?.map(att => ({
    ...att,
    download_url: att.download_url || (att.file_id ? `/files/${att.file_id}` : undefined),
  }));
  const histCitations = Array.isArray(m.metadata?.citations)
    ? m.metadata.citations as CitationItem[]
    : Array.isArray(m.citations)
    ? m.citations as CitationItem[]
    : undefined;
  const histFollowUps = Array.isArray(m.metadata?.follow_up_questions)
    ? m.metadata.follow_up_questions as string[]
    : undefined;
  const histQuotedFollowUp = m.role === 'user' && m.metadata?.quoted_follow_up && typeof m.metadata.quoted_follow_up === 'object'
    ? {
      text: String((m.metadata.quoted_follow_up as Record<string, unknown>).text ?? ''),
      ts: Number((m.metadata.quoted_follow_up as Record<string, unknown>).ts ?? 0) || undefined,
    }
    : undefined;
  const histWorkspaceFiles = Array.isArray(m.metadata?.workspace_files)
    ? (m.metadata.workspace_files as unknown[])
        .filter((x): x is string => typeof x === 'string' && x.trim().length > 0)
    : undefined;
  const rawEvolution = m.role === 'assistant'
    && m.metadata?.evolution
    && typeof m.metadata.evolution === 'object'
    ? m.metadata.evolution as Partial<EvolutionSummary>
    : undefined;
  const histEvolution: EvolutionSummary | undefined = rawEvolution
    && ['pending', 'settled', 'failed', 'empty'].includes(String(rawEvolution.state))
    ? rawEvolution as EvolutionSummary
    : undefined;
  const rawOntologyGovernance = m.role === 'assistant'
    && m.metadata?.ontology_governance
    && typeof m.metadata.ontology_governance === 'object'
    ? m.metadata.ontology_governance as Partial<OntologyGovernanceSummary>
    : undefined;
  const histOntologyGovernance: OntologyGovernanceSummary | undefined = rawOntologyGovernance
    ? {
        governance_run_id: rawOntologyGovernance.governance_run_id,
        activations: Array.isArray(rawOntologyGovernance.activations) ? rawOntologyGovernance.activations : [],
        gates: Array.isArray(rawOntologyGovernance.gates) ? rawOntologyGovernance.gates : [],
        review: rawOntologyGovernance.review && typeof rawOntologyGovernance.review === 'object'
          ? rawOntologyGovernance.review
          : {},
        revision: (rawOntologyGovernance.review
          && typeof rawOntologyGovernance.review === 'object'
          && typeof rawOntologyGovernance.review.candidate_answer === 'string'
          && rawOntologyGovernance.review.candidate_answer)
          || revisionToolCalls.length > 0
          ? {
              status: 'completed',
              content: typeof rawOntologyGovernance.review?.candidate_answer === 'string'
                ? rawOntologyGovernance.review.candidate_answer
                : '',
              thinking: [],
              toolCalls: revisionToolCalls,
              toolCallCount: revisionToolCalls.length,
            }
          : undefined,
      }
    : undefined;

  // Citation badges (/skills, /plugins, @sub-agents) are rebuilt from extra_data so they still show after a history session refresh.
  const histSkillName = m.role === 'user' && typeof m.metadata?.skill_name === 'string'
    ? m.metadata.skill_name as string : undefined;
  const histSkillId = m.role === 'user' && typeof m.metadata?.skill_id === 'string'
    ? m.metadata.skill_id as string : undefined;
  const histPluginName = m.role === 'user' && typeof m.metadata?.plugin_name === 'string'
    ? m.metadata.plugin_name as string : undefined;
  const histConnectorName = m.role === 'user' && typeof m.metadata?.connector_name === 'string'
    ? m.metadata.connector_name as string : undefined;
  const histMentionName = m.role === 'user' && typeof m.metadata?.mention_name === 'string'
    ? m.metadata.mention_name as string : undefined;
  // The @sub-agent routing prefix was written into the persisted body ("@name body"); when there's a mention badge, strip it,
  // otherwise the body would display it once more, duplicating the badge.
  if (histMentionName) {
    const prefix = `@${histMentionName} `;
    if (cleanContent.startsWith(prefix)) cleanContent = cleanContent.slice(prefix.length);
    else if (cleanContent === `@${histMentionName}`) cleanContent = '';
  }

  return {
    role: (m.role === 'assistant' ? 'assistant' : 'user') as 'user' | 'assistant',
    content: cleanContent,
    isMarkdown: !!(m.metadata?.is_markdown),
    ts: m.created_at ? new Date(m.created_at).getTime() : Date.now(),
    toolCalls,
    segments,
    ...(histCitations && histCitations.length > 0 && { citations: histCitations }),
    ...(histFollowUps && histFollowUps.length > 0 && { followUpQuestions: histFollowUps }),
    ...(histAttachments && histAttachments.length > 0 && { attachments: histAttachments }),
    ...(histQuotedFollowUp?.text && { quotedFollowUp: histQuotedFollowUp }),
    ...(histWorkspaceFiles !== undefined && { workspaceFiles: histWorkspaceFiles }),
    ...(histEvolution && { evolution: histEvolution }),
    ...(histOntologyGovernance && { ontologyGovernance: histOntologyGovernance }),
    ...(histSkillId && { skillId: histSkillId }),
    ...(histSkillName && { skillName: histSkillName }),
    ...(histPluginName && { pluginName: histPluginName }),
    ...(histConnectorName && { connectorName: histConnectorName }),
    ...(histMentionName && { mentionName: histMentionName }),
    ...(typeof m.message_id === 'string' && m.message_id && { messageId: m.message_id }),
    ...(m.role === 'assistant' && typeof m.metadata?.duration_ms === 'number' && m.metadata.duration_ms >= 0
      && { durationMs: m.metadata.duration_ms }),
  } as ChatMessage;
}

export function useChatInit() {
  const { authUser, authExpiredUrl, authChecking, initAuth } = useAuthStore();
  const { loadMemorySettings, loadOntologySettings } = useSettingsStore();
  const { setFeatureUpdates } = useUIStore();
  const {
    updateStore, setCurrentChatId, setChatsLoading, setToolDisplayNames,
    addBackendSessionId, clearBackendSessionIds,
    addLoadedMsgId, clearLoadedMsgIds,
    currentChatId, sessionLoadEpoch, bumpSessionLoadEpoch,
    hydrateForUser,
  } = useChatStore();
  const { catalog, setCatalog, setCatalogLoading, panel, setPanel } = useCatalogStore();

  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Auth initialization
  useEffect(() => { initAuth(); }, []);

  // Load memory settings when auth is ready
  useEffect(() => {
    if (!authUser) return;
    loadMemorySettings();
    loadOntologySettings();
  }, [authUser]);

  // Proactive session heartbeat
  useEffect(() => {
    if (!authUser) return;
    let lastCheck = Date.now();
    const SESSION_CHECK_INTERVAL = 30_000;
    let checking = false;

    const onInteraction = async () => {
      if (authExpiredUrl) return;
      const now = Date.now();
      if (now - lastCheck < SESSION_CHECK_INTERVAL) return;
      if (checking) return;
      checking = true;
      lastCheck = now;
      try {
        // Refresh capability bits while renewing: after an admin changes user/team/role permissions, an already-logged-in user
        // syncs within ~30s without re-logging in (only written back when there's an actual change, to avoid needless re-renders).
        const fresh = await checkSession();
        const cur = useAuthStore.getState().authUser;
        if (fresh && cur && JSON.stringify(fresh) !== JSON.stringify(cur)) {
          useAuthStore.getState().setAuthUser(fresh);
        }
      } catch { /* session invalidation is handled by the global 401 handler */ } finally { checking = false; }
    };

    document.addEventListener('click', onInteraction, { capture: true });
    document.addEventListener('keydown', onInteraction, { capture: true });
    return () => {
      document.removeEventListener('click', onInteraction, { capture: true });
      document.removeEventListener('keydown', onInteraction, { capture: true });
    };
  }, [authUser, authExpiredUrl]);

  // Fetch docs content
  useEffect(() => {
    if (authChecking || !authUser) return;
    if (panel !== 'docs') return;
    authFetch(`${effectiveApiUrl}/v1/content/docs`)
      .then(r => r.json())
      .then(data => {
        if (Array.isArray(data?.data?.updates)) setFeatureUpdates(data.data.updates as UpdateEntry[]);
      })
      .catch(() => {});
  }, [panel, authChecking, authUser, setFeatureUpdates]);

  // Persist catalog
  useEffect(() => {
    saveCatalog(catalog);
  }, [catalog]);

  // Refresh catalog from backend
  const refreshCatalog = async () => {
    setCatalogLoading(true);
    try {
      const r = await authFetch(`${effectiveApiUrl}/v1/catalog`, { method: 'GET' });
      if (!r.ok) { setCatalogLoading(false); return; }
      const payload = await r.json();
      const remote = payload?.data ?? payload;
      if (!remote || typeof remote !== 'object') { setCatalogLoading(false); return; }
      const next: Catalog = {
        skills: Array.isArray(remote.skills) ? remote.skills : [],
        agents: Array.isArray(remote.agents) ? remote.agents : [],
        mcp: Array.isArray(remote.mcp) ? remote.mcp : [],
        kb: Array.isArray(remote.kb) ? remote.kb : [],
      };
      setCatalog(next);
    } catch {} finally {
      setCatalogLoading(false);
    }
  };

  useEffect(() => {
    if (authChecking || !authUser) return;
    refreshCatalog();
  }, [effectiveApiUrl, authUser, authChecking]);

  // Fetch tool display names
  useEffect(() => {
    if (authChecking || !authUser) return;
    authFetch(`${effectiveApiUrl}/v1/config/tool-names`)
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (data && typeof data.tools === 'object') {
          setToolDisplayNames({ ...data.tools, ...(data.servers || {}) });
        }
      })
      .catch(() => {});
  }, [effectiveApiUrl, authChecking, authUser]);

  // Load chat sessions from backend
  // Use authUser?.user_id (not the full authUser object) so that updating only
  // the avatar URL does not trigger a re-fetch and panel navigation.
  const authUserId = authUser?.user_id ?? null;
  useEffect(() => {
    if (authChecking || !authUserId) return;
    if (!effectiveApiUrl) return;
    // Switch the chat store into this user's context BEFORE we read its
    // snapshot — without this, the snapshot would be either empty (initial
    // boot) or, worse, the previous user's data still in memory after a
    // login swap.
    hydrateForUser(authUserId);
    useAutomationChatStore.getState().hydrateForUser(authUserId);
    // 侧边栏手动拖拽顺序：本地秒开 + 异步拉服务端顺序（见 sidebarOrderStore）
    useSidebarOrderStore.getState().hydrateForUser(authUserId);
    const localSnapshot = useChatStore.getState().store;
    clearBackendSessionIds();
    clearLoadedMsgIds();

    let cancelled = false;

    const fetchSessions = async () => {
      setChatsLoading(true);
      try {
        const r = await authFetch(`${effectiveApiUrl}/v1/chats?page_size=100&exclude_automation=true`);
        if (!r.ok || cancelled) return;
        const payload = await r.json();
        const items: any[] = payload?.data?.items || [];
        // 双模式：并入本机执行面上「本地项目」的会话（本机未就绪时静默跳过），
        // 并登记 chat→local，后续消息/操作请求自动路由到本机。
        if (isHybridDual()) {
          try {
            const lr = await authFetch(`${effectiveApiUrl}/v1/chats?page_size=100&exclude_automation=true`, {
              headers: { [LOCAL_TARGET_HEADER]: 'local' },
            });
            if (lr.ok && !cancelled) {
              const lp = await lr.json();
              const localItems: any[] = lp?.data?.items || [];
              localItems.forEach((it) => registerLocalChat(it.chat_id));
              items.push(...localItems);
            }
          } catch { /* 本机执行面未就绪：仅展示云端会话 */ }
        }
        const chats: Record<string, ChatItem> = {};
        const order: string[] = [];

        for (const s of items) {
          const id: string = s.chat_id;
          const meta = (s.metadata || {}) as any;
          // 手动重命名保护：后端已带 title_manually_set 直接用；本地改过名但还没
          // 同步到后端（流式期间改名后刷新）→ 保留本地标题，后续流结束时自动补同步
          const localManual = localSnapshot.chats[id]?.titleManuallySet === true;
          const backendManual = meta.title_manually_set === true;
          const preservedTitle = !backendManual && localManual && localSnapshot.chats[id]?.title
            ? localSnapshot.chats[id].title
            : (s.title || '新对话');
          chats[id] = {
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
            ...(typeof localSnapshot.chats[id]?.planModeActive === 'boolean'
              ? { planModeActive: localSnapshot.chats[id].planModeActive }
              : {}),
            batchChat: meta.batch_chat === true ? true : undefined,
            ...(typeof localSnapshot.chats[id]?.batchModeActive === 'boolean'
              ? { batchModeActive: localSnapshot.chats[id].batchModeActive }
              : {}),
            workflowChat: meta.workflow_chat === true ? true : undefined,
            ...(typeof localSnapshot.chats[id]?.workflowModeActive === 'boolean'
              ? { workflowModeActive: localSnapshot.chats[id].workflowModeActive }
              : {}),
            automationTaskId: typeof meta.automation_task_id === 'string' ? meta.automation_task_id : undefined,
            automationRun: meta.automation_run === true ? true : undefined,
            planProgress: toPlanProgress(meta.plan_progress),
            // When the backend session hasn't bound project_id (e.g. bound locally via the input-box dropdown, not yet persisted with a message),
            // keep the locally bound projectId/projectName — otherwise the session would fall back to the default project after refresh. The next send
            // carries project_id and self-heals into the DB.
            projectId: (typeof s.project_id === 'string' && s.project_id)
              ? s.project_id
              : (localSnapshot.chats[id]?.projectId || undefined),
            projectName: localSnapshot.chats[id]?.projectName || undefined,
          };
          order.push(id);
          addBackendSessionId(id);
        }

        if (!cancelled) {
          // Capture previously selected chat before updating store
          const prevChatId = useChatStore.getState().currentChatId;

          updateStore((prev) => {
            // Session fetches are asynchronous. Preserve the freshest explicit composer choice
            // from the live store as well as the startup snapshot, so a click made while this
            // request was in flight cannot be overwritten by the server response.
            const mergedServerChats: Record<string, ChatItem> = {};
            for (const [id, serverChat] of Object.entries(chats)) {
              const active = prev.chats[id]?.planModeActive;
              const batchActive = prev.chats[id]?.batchModeActive;
              mergedServerChats[id] = {
                ...serverChat,
                ...(typeof active === 'boolean' ? { planModeActive: active } : {}),
                ...(typeof batchActive === 'boolean' ? { batchModeActive: batchActive } : {}),
              };
            }
            const preserved: Record<string, ChatItem> = {};
            const preservedOrder: string[] = [];
            for (const id of localSnapshot.order) {
              const localChat = localSnapshot.chats[id];
              const hasMessages = Array.isArray(localChat?.messages) && localChat.messages.length > 0;
              if (!mergedServerChats[id] && localChat && hasMessages && !isAutomationHistoryChat(localChat)) {
                preserved[id] = localChat;
                preservedOrder.push(id);
              }
            }
            return {
              chats: { ...mergedServerChats, ...preserved },
              order: [...order, ...preservedOrder],
            };
          });

          // On a fresh login (SSO ticket exchange) always land on the home page
          // (chat panel + a brand-new empty chat → recommend banner). On a plain
          // browser refresh, restore the previously-selected chat and keep the
          // user on whichever panel they were on (sub-agents / knowledge base / app center / my space, etc.).
          const isFreshLogin = typeof window !== 'undefined'
            && window.sessionStorage.getItem(LOGIN_LANDING_KEY) === '1';
          const allChats = { ...chats };
          // 恢复目标：非新登录一律保留原会话 id（问题14/17）。后端已有 → 恢复
          // 历史；后端没有（正在流式输出首条消息、或本地空会话）→ 保留同一 id：
          // 空会话渲染出来就是空首页，与生成新 id 的 UX 等价，但指针稳定——
          // 不会把新 id 写回共享 localStorage 去覆盖别的标签页的恢复目标。
          const targetChatId = isFreshLogin ? nowId('chat') : (prevChatId || nowId('chat'));
          if (isFreshLogin) setPanel('chat');
          setCurrentChatId(targetChatId);
          // Bump epoch so the lazy-load messages effect re-fires even when
          // currentChatId hasn't changed (e.g. page refresh restores the
          // same chat ID from localStorage).
          bumpSessionLoadEpoch();
          // Clean up legacy login landing flag if present
          if (typeof window !== 'undefined') {
            window.sessionStorage.removeItem(LOGIN_LANDING_KEY);
          }

          // Pre-load messages for the target chat BEFORE clearing chatsLoading,
          // so the user never sees the empty home page flash.
          if (
            allChats[targetChatId]
            && !useChatStore.getState().loadedMsgIds.has(targetChatId)
            && !inflightMsgLoads.has(targetChatId)
          ) {
            inflightMsgLoads.add(targetChatId);
            // Only mark the chat "loaded" when this quick single-page fetch
            // fully covered it. On failure (non-2xx, network error) or when
            // more pages exist, re-bump the epoch so the lazy-load effect
            // actually retries — otherwise the chat is stuck on the skeleton
            // until the next full refresh.
            let preloadComplete = false;
            try {
              const mr = await authFetch(`${effectiveApiUrl}/v1/chats/${targetChatId}/messages?page=1&page_size=100`, { headers: { ...chatTargetHeaders(targetChatId) } });
              if (mr.ok && !cancelled) {
                const mp = await mr.json();
                const msgItems: any[] = mp?.data?.items || [];
                const quickMsgs: ChatMessage[] = markResolvedPlanPreviews(msgItems.map(parseHistoryMessage));
                if (Object.prototype.hasOwnProperty.call(mp?.data || {}, 'context_usage')) {
                  useChatStore.getState().setContextUsage(
                    targetChatId,
                    parseContextUsageSnapshot(mp.data.context_usage),
                  );
                }
                if (Object.prototype.hasOwnProperty.call(mp?.data || {}, 'context_compaction')) {
                  useChatStore.getState().setContextCompaction(
                    targetChatId,
                    parseContextCompactionState(mp.data.context_compaction),
                  );
                }
                preloadComplete = !mp?.data?.pagination?.has_next;
                if (!cancelled && quickMsgs.length > 0) {
                  // Detect plan mode from message content (fallback for sessions
                  // created before plan_chat metadata was persisted)
                  const hasPlanMessages = msgItems.some((m: any) => m.metadata?.plan_snapshot);
                  // If the most recent plan_snapshot is still in 'preview' mode,
                  // the plan was generated but never executed — restore
                  // currentPlanId so a follow-up "确认执行" triggers Phase 2
                  // (execute) instead of Phase 1 (generate a new plan).
                  let pendingPlanId: string | null = null;
                  for (const m of msgItems) {
                    const snap = m?.metadata?.plan_snapshot;
                    if (m?.role !== 'assistant' || !snap) continue;
                    const mode = (snap as any).mode || 'complete';
                    const pid = m?.metadata?.plan_id;
                    pendingPlanId = mode === 'preview' && pid ? String(pid) : null;
                  }
                  updateStore(prev => {
                    const c = prev.chats[targetChatId];
                    if (!c) return prev;
                    return {
                      ...prev,
                      chats: {
                        ...prev.chats,
                        [targetChatId]: {
                          ...c,
                          messages: quickMsgs,
                          ...(hasPlanMessages && !c.planChat ? { planChat: true } : {}),
                        },
                      },
                    };
                  });
                  // Legacy sessions may lack plan_chat metadata, so their history remains a
                  // fallback for the initial mode. An explicit false is the user's persisted
                  // choice to continue as an ordinary conversation and must never be overwritten.
                  const latestState = useChatStore.getState();
                  const latestChat = latestState.store.chats[targetChatId];
                  if (
                    hasPlanMessages
                    && targetChatId === latestState.currentChatId
                    && shouldRestorePlanModeFromHistory(latestChat)
                    && !latestState.planMode
                  ) {
                    latestState.setPlanMode(true);
                  }
                  if (pendingPlanId) {
                    useChatStore.getState().setCurrentPlanId(pendingPlanId);
                  }
                }
              }
            } catch { /* ignore — lazy-load retries via the epoch bump below */ }
            inflightMsgLoads.delete(targetChatId);
            if (!cancelled) {
              if (preloadComplete) {
                addLoadedMsgId(targetChatId);
              } else {
                // The lazy-load effect already ran (and skipped) while this
                // preload held the in-flight lock; bump the epoch so it
                // re-fires now that the lock is released.
                bumpSessionLoadEpoch();
              }
            }
          }
        }
      } catch {} finally {
        if (!cancelled) setChatsLoading(false);
      }
    };

    fetchSessions();

    // Load sidebar-activated automation tasks (non-blocking)
    const fetchSidebarAutomations = async () => {
      try {
        const r = await authFetch(`${effectiveApiUrl}/v1/automations?sidebar_activated=true`);
        if (!r.ok || cancelled) return;
        const payload = await r.json();
        const tasks = payload?.data || [];
        useAutomationChatStore.getState().setSidebarTasks(tasks);
      } catch { /* ignore — sidebar automation entries are optional */ }
    };
    fetchSidebarAutomations();

    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effectiveApiUrl, authUserId, authChecking]);

  // 计划栏还原 —— 计划清单的真源在服务端（会话 metadata.plan_progress）。
  //
  // 计划栏本身是内存态，刷新就没；而工作流模式下一份计划要跨好几轮才走完（提交作业 →
  // 后台跑几十分钟 → 作业跑完的交付轮收尾）。中途刷新、切走再回来、或者干脆关了页面
  // 第二天再看，过去都只能看到"什么都没有"，或者停在离开时那一步的转圈。这里按服务端
  // 快照恢复：包括它是否已经收尾（settled → done），所以收尾也不再依赖"当时这个标签页
  // 恰好在跟那条流"。本地那份更新（正在跟流）优先，别把实时进度盖回旧快照。
  useEffect(() => {
    const chatId = currentChatId;
    if (!chatId) return;
    const st = useChatStore.getState();
    const persisted = st.store.chats[chatId]?.planProgress;
    if (!persisted) return;
    const live = st.planProgress[chatId];
    if (live && live.updatedAt >= persisted.updatedAt) return;
    st.setPlanProgress(chatId, persisted);
  }, [currentChatId, sessionLoadEpoch]);

  // Lazy-load messages for current chat
  useEffect(() => {
    if (authChecking || !authUser) return;
    const chatId = currentChatId;
    const state = useChatStore.getState();
    if (state.loadedMsgIds.has(chatId)) return;
    // Another run (or the startup preload) is already fetching this chat.
    // If that run gets cancelled it re-bumps the epoch, so skipping here
    // never strands the chat.
    if (inflightMsgLoads.has(chatId)) return;

    let cancelled = false;

    // If this chat isn't in the backend session list we fetched on startup,
    // it might be an automation-generated chat, a notification-linked chat,
    // or any chat we haven't "seen" before. Try to hydrate its session
    // metadata from GET /v1/chats/{id} before loading messages.
    // If the chat also isn't in the local store (i.e. a brand-new local chat
    // the user hasn't typed into yet), the 404 response short-circuits us.
    const hydrateSessionIfMissing = async (): Promise<boolean> => {
      if (state.backendSessionIds.has(chatId)) return true;
      const localChat = state.store.chats[chatId];
      if (localChat && localChat.messages.length > 0) {
        // Local-only chat with content — don't hit backend
        return false;
      }
      try {
        const sr = await authFetch(`${effectiveApiUrl}/v1/chats/${chatId}`);
        if (!sr.ok || cancelled) return false;
        const sp = await sr.json();
        const s = sp?.data;
        if (!s || !s.chat_id) return false;
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const meta = (s.metadata || {}) as any;
        updateStore(prev => {
          const newChatItem: ChatItem = {
            id: s.chat_id,
            title: s.title || '新对话',
            ...(meta.title_manually_set === true ? { titleManuallySet: true } : {}),
            createdAt: s.created_at ? new Date(s.created_at).getTime() : Date.now(),
            updatedAt: s.updated_at ? new Date(s.updated_at).getTime() : Date.now(),
            messages: [],
            favorite: !!s.favorite,
            pinned: !!s.pinned,
            businessTopic: meta.businessTopic || '综合咨询',
            agentId: meta.agent_id || undefined,
            agentName: meta.agent_name || undefined,
            planChat: meta.plan_chat === true ? true : undefined,
            ...(typeof prev.chats[s.chat_id]?.planModeActive === 'boolean'
              ? { planModeActive: prev.chats[s.chat_id].planModeActive }
              : {}),
            batchChat: meta.batch_chat === true ? true : undefined,
            ...(typeof prev.chats[s.chat_id]?.batchModeActive === 'boolean'
              ? { batchModeActive: prev.chats[s.chat_id].batchModeActive }
              : {}),
            automationTaskId: typeof meta.automation_task_id === 'string' ? meta.automation_task_id : undefined,
            automationRun: meta.automation_run === true ? true : undefined,
            workflowChat: meta.workflow_chat === true ? true : undefined,
            planProgress: toPlanProgress(meta.plan_progress),
            // Same as above: when the backend hasn't bound project_id, keep the local binding to avoid falling back to the default project on refresh.
            projectId: (typeof s.project_id === 'string' && s.project_id)
              ? s.project_id
              : (prev.chats[s.chat_id]?.projectId || undefined),
            projectName: prev.chats[s.chat_id]?.projectName || undefined,
          };
          return {
            ...prev,
            chats: { ...prev.chats, [s.chat_id]: newChatItem },
            order: prev.order.includes(s.chat_id) ? prev.order : [s.chat_id, ...prev.order],
          };
        });
        addBackendSessionId(chatId);
        return true;
      } catch {
        return false;
      }
    };

    const fetchMessages = async () => {
      const hydrated = await hydrateSessionIfMissing();
      if (!hydrated || cancelled) return;
      // Take the in-flight lock; the "loaded" store mark is only set after
      // the messages are actually written. Keeping the two separate matters:
      // the mark drives the skeleton-vs-empty-state UI, so setting it during
      // the fetch would flash the empty home page while messages load, and
      // a cancelled/failed load must leave the mark unset so the next visit
      // retries instead of being stuck on the skeleton forever.
      inflightMsgLoads.add(chatId);
      let loaded = false;
      try {
        let page = 1;
        const allMessages: ChatMessage[] = [];
        let hasPlanMessages = false;
        // Track the latest plan_snapshot across all pages; if its mode remains
        // 'preview' by the end, restore currentPlanId so "确认执行" after
        // refresh executes the existing plan instead of generating a new one.
        let pendingPlanId: string | null = null;
        let contextCompactionSeen = false;
        let contextCompaction: ContextCompactionState | null = null;
        let contextUsageSeen = false;
        let contextUsage: ContextUsageSnapshot | null = null;

        while (true) {
          const r = await authFetch(`${effectiveApiUrl}/v1/chats/${chatId}/messages?page=${page}&page_size=100`, { headers: { ...chatTargetHeaders(chatId) } });
          if (cancelled) return;
          // A non-2xx page (401 blip, 502 during backend restart, 429…) must
          // NOT fall through to the store write below — that would persist an
          // empty/partial message list under a "loaded" mark. Throw so the
          // finally-rollback lets the next visit retry.
          if (!r.ok) throw new Error(`messages page ${page}: HTTP ${r.status}`);
          const payload = await r.json();
          const items: any[] = payload?.data?.items || [];
          if (!contextCompactionSeen && Object.prototype.hasOwnProperty.call(payload?.data || {}, 'context_compaction')) {
            contextCompactionSeen = true;
            contextCompaction = parseContextCompactionState(payload.data.context_compaction);
          }
          if (!contextUsageSeen && Object.prototype.hasOwnProperty.call(payload?.data || {}, 'context_usage')) {
            contextUsageSeen = true;
            contextUsage = parseContextUsageSnapshot(payload.data.context_usage);
          }

          for (const m of items) {
            const planSnapshot = m?.metadata?.plan_snapshot;
            if (m?.role === 'assistant' && planSnapshot) {
              hasPlanMessages = true;
              const mode = (planSnapshot as any).mode || 'complete';
              const pid = m.metadata?.plan_id;
              pendingPlanId = mode === 'preview' && pid ? String(pid) : null;
            }
            allMessages.push(parseHistoryMessage(m));
          }

          const pagination = payload?.data?.pagination;
          if (!pagination?.has_next) break;
          page++;
        }

        if (!cancelled) {
          if (contextUsageSeen) {
            useChatStore.getState().setContextUsage(chatId, contextUsage);
          }
          if (contextCompactionSeen) {
            useChatStore.getState().setContextCompaction(chatId, contextCompaction);
          }
          loaded = true;
          msgLoadRetryCounts.delete(chatId);
          addLoadedMsgId(chatId);
          updateStore(prev => {
            const c = prev.chats[chatId];
            if (!c) return prev;
            return {
              ...prev,
              chats: {
                ...prev.chats,
                [chatId]: {
                  ...c,
                  messages: markResolvedPlanPreviews(allMessages),
                  ...(hasPlanMessages && !c.planChat ? { planChat: true } : {}),
                },
              },
            };
          });
          // Sync the legacy default only when the user has not explicitly disabled plan mode.
          const latestState = useChatStore.getState();
          const latestChat = latestState.store.chats[chatId];
          if (
            hasPlanMessages
            && chatId === latestState.currentChatId
            && shouldRestorePlanModeFromHistory(latestChat)
            && !latestState.planMode
          ) {
            latestState.setPlanMode(true);
          }
          // Restore pending plan id (if any) for the active chat
          if (pendingPlanId && chatId === useChatStore.getState().currentChatId) {
            useChatStore.getState().setCurrentPlanId(pendingPlanId);
          }
        }
      } catch {
        // HTTP/网络失败：有限次自动重试（问题16：历史对话长时间停在骨架屏）。
        // 超过上限后放弃，等用户下次切入该会话再试。
        if (!cancelled) {
          const attempts = (msgLoadRetryCounts.get(chatId) || 0) + 1;
          msgLoadRetryCounts.set(chatId, attempts);
          if (attempts <= MSG_LOAD_MAX_RETRIES) {
            window.setTimeout(() => {
              const st = useChatStore.getState();
              if (st.currentChatId === chatId && !st.loadedMsgIds.has(chatId)) {
                st.bumpSessionLoadEpoch();
              }
            }, 1500 * attempts);
          }
        }
      } finally {
        inflightMsgLoads.delete(chatId);
        // Switch-back race: if the user already navigated back to this chat
        // while this cancelled run still held the in-flight lock, that
        // navigation's effect run skipped on the lock and nobody will
        // refetch. Re-fire the effect now that the lock is released. Only on
        // cancellation — an HTTP failure must not self-retry in a loop.
        if (!loaded && cancelled && useChatStore.getState().currentChatId === chatId) {
          bumpSessionLoadEpoch();
        }
      }
    };

    fetchMessages();
    return () => { cancelled = true; };
  }, [currentChatId, effectiveApiUrl, authUser, authChecking, sessionLoadEpoch]);

  // ── Reconnect / hydrate batch executions when this chat opens ──
  // - Active plans (confirmed/running): re-attach the SSE stream; the
  //   orchestrator runs as a detached server-side task so refresh
  //   doesn't kill it.
  // - Finished plans (done/failed): hydrate the panel directly from
  //   plan.item_results — no fake "in-progress" pulse, no replay flicker.
  useEffect(() => {
    if (authChecking || !authUser) return;
    const chatId = currentChatId;
    if (!chatId) return;
    let cancelled = false;
    (async () => {
      try {
        const plans = await listActiveBatchPlans(chatId);
        if (cancelled) return;
        // Hydrate finished plans + reconnect to running ones in parallel
        // so users with multiple historical batches don't wait for N
        // sequential GETs.
        await Promise.all(plans.map(async (p) => {
          const meta: BatchPlanMeta = {
            plan_id: p.plan_id,
            total: p.items_total,
            source_type: p.source_type as BatchSourceType,
            preview: p.items_preview as Record<string, unknown>[],
            default_template: p.prompt_template,
            placeholder_keys: p.placeholder_keys,
            chat_id: chatId,
          };

          if (p.status !== 'done' && p.status !== 'failed') {
            useBatchStore.getState().connectStream(p.plan_id, meta);
            return;
          }
          // Finished — pull the per-item results directly and render
          // as a static snapshot. Saves an SSE round-trip and keeps
          // the sidebar pulse off (work is already done).
          try {
            const detail = await getBatchPlan(p.plan_id);
            if (cancelled) return;
            const results = (detail.item_results || []).map((r) => ({
              index: r.index,
              status: r.status,
              content: r.content,
              error: r.error,
              retry_count: r.retry_count,
              // Backend returns these as opaque arrays; BatchItemBubble
              // normalizes snake_case → camelCase per item.
              tool_calls: (Array.isArray(r.tool_calls) ? r.tool_calls : undefined) as BatchItemResult['tool_calls'],
              artifacts: Array.isArray(r.artifacts) ? r.artifacts : undefined,
              citations: (Array.isArray(r.citations) ? r.citations : undefined) as BatchItemResult['citations'],
            } satisfies BatchItemResult));
            useBatchStore.getState().hydratePlan(
              meta,
              p.status as 'done' | 'failed',
              results,
              { success: detail.progress?.success ?? 0, failed: detail.progress?.failed ?? 0 },
            );
          } catch {
            // best-effort — chat still works without this plan
          }
        }));
      } catch {
        // best-effort — chat still works without reattach
      }
    })();
    return () => { cancelled = true; };
  }, [currentChatId, authUser, authChecking]);

  return {
    effectiveApiUrl,
    refreshCatalog,
    searchTimerRef,
  };
}
