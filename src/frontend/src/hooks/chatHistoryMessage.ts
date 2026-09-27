import { t } from '../i18n';
import { buildHistorySegments } from '../utils/segments';
import { attachArtifactsToToolCalls } from '../utils/fileParser';
import { stripMcpToolPrefix } from '../utils/constants';
import type { ForkedHistoryMessage } from '../utils/forkHistory';
import type { CitationItem, EvolutionSummary, OntologyGovernanceSummary, StoredSegment, ThinkingBlock, ToolCall, ReferencedChatCard } from '../types';

// Convert a backend message item (from GET /v1/chats/{id}/messages) into a
// frontend ChatMessage. Pure — used by both the preload path (during initial
// session fetch) and the lazy-load path (when switching into a never-loaded
// chat). Adding new fields persisted in metadata? Add them here, both paths
// pick it up automatically.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function parseHistoryMessage(m: any): ForkedHistoryMessage {
  const inherited = m.metadata?.forked_history === true;
  const historicalUsage = inherited ? m.metadata?.forked_usage ?? m.usage : undefined;
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
        // 既没有 status 也没有 result 的条目 = 这次调用的结果从来没落库。以前一律
        // 当成功读回来，于是"结果丢了"在历史里长得和"成功但没输出"一模一样；
        // 照实标成中断，异常才看得见。（老历史里结果在、只是没写 status 的条目
        // 仍然算成功。）
        status: (tc.status === 'error'
          ? 'error'
          : tc.status === 'interrupted'
            ? 'interrupted'
            : !tc.status && (tc.result ?? tc.output) === undefined
              ? 'interrupted'
              : 'success') as 'success' | 'error' | 'interrupted',
        // 后端把开始时刻与耗时一并落库，历史因此能还原出这次调用真实占了多久；
        // 拿不到就不给值，让卡片不显示耗时，而不是拿"这张卡刚画出来"当起点。
        timestamp: typeof tc.started_at === 'number' ? tc.started_at : tc.timestamp,
        ...(typeof tc.duration_ms === 'number' ? { durationMs: tc.duration_ms } : {}),
        // sub-agent internal process (thinking + tool calls) — replayed from the DB after refresh
        ...(Array.isArray(tc.sub_steps ?? tc.subSteps)
          ? { subSteps: (tc.sub_steps ?? tc.subSteps) }
          : {}),
        ...(tc.subagent_name ?? tc.subagentName
          ? { subagentName: tc.subagent_name ?? tc.subagentName }
          : {}),
        ...(typeof tc.scope === 'string' ? { scope: tc.scope } : {}),
        // 历史列表只给了梗概；展开这张卡时 ToolCallRow 会按需回取完整结果。
        ...(tc.result_truncated === true ? { outputTruncated: true } : {}),
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
  // 展示顺序由后端在流式那一刻记下，落在 metadata.segments；这里原样取出。
  const storedSegments = Array.isArray(m.metadata?.segments)
    ? (m.metadata.segments as StoredSegment[])
    : undefined;
  let { segments, cleanContent } = m.role === 'assistant'
    ? buildHistorySegments(rawContent, toolCalls, storedThinking, storedSegments)
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
        planId: !inherited && m.metadata?.plan_id ? String(m.metadata.plan_id) : undefined,
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
    const textSegments = (inherited || snap.mode === 'complete') && snap.result_text
      ? [{ type: 'text' as const, content: String(snap.result_text) }]
      : [];
    segments = [planSeg, ...toolSegs, ...textSegments];
    cleanContent = (inherited || snap.mode === 'complete') && snap.result_text
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
  const histReferencedChats = m.role === 'user' && Array.isArray(m.metadata?.referenced_chats)
    ? (m.metadata.referenced_chats as ReferencedChatCard[])
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
    ...(inherited ? { forkedHistory: true } : {}),
    ...(historicalUsage && typeof historicalUsage === 'object' && !Array.isArray(historicalUsage) ? { usage: historicalUsage } : {}),
    role: (m.role === 'assistant' ? 'assistant' : 'user') as 'user' | 'assistant',
    content: cleanContent,
    isMarkdown: !!(m.metadata?.is_markdown),
    // message_id 是 chat_messages 的主键，历史消息一定带；它就是这条消息的身份。
    uid: String(m.message_id),
    ts: m.created_at ? new Date(m.created_at).getTime() : Date.now(),
    toolCalls,
    segments,
    ...(storedThinking?.length ? { thinking: storedThinking } : {}),
    ...(histCitations && histCitations.length > 0 && { citations: histCitations }),
    ...(histFollowUps && histFollowUps.length > 0 && { followUpQuestions: histFollowUps }),
    ...(histAttachments && histAttachments.length > 0 && { attachments: histAttachments }),
    ...(histQuotedFollowUp?.text && { quotedFollowUp: histQuotedFollowUp }),
    ...(histReferencedChats && histReferencedChats.length > 0 && { referencedChats: histReferencedChats }),
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
    // 后端在轮次接纳时就建好了这一行、跑的过程中持续刷新：带 in_flight 的就是
    // 正在跑的那一轮。按活气泡渲染，跟随流的时候从 event_offset 接着补，不从头重放。
    ...(m.role === 'assistant' && !inherited && m.in_flight && {
      isStreaming: true,
      inFlight: {
        ...(typeof m.in_flight.event_offset === 'number' && { eventOffset: m.in_flight.event_offset as number }),
      },
    }),
    ...(m.role === 'assistant' && m.error && typeof m.error.error === 'string' && { error: m.error.error as string }),
  } as ForkedHistoryMessage;
}
