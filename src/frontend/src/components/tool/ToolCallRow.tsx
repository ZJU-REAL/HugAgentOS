import { useEffect, useMemo, useRef, useState } from 'react';
import type { ComponentType } from 'react';
import {
  CloseOutlined,
  LoadingOutlined,
  SearchOutlined,
  GlobalOutlined,
  FileTextOutlined,
  EditOutlined,
  CodeOutlined,
  DatabaseOutlined,
  ThunderboltOutlined,
  RobotOutlined,
  FolderOutlined,
  BarChartOutlined,
  FileWordOutlined,
  FileExcelOutlined,
} from '@ant-design/icons';
import type { ToolCall } from '../../types';
import { TOOL_NAME_OVERRIDES } from '../../utils/constants';
import { useChatStore, useUIStore } from '../../stores';
import { ElapsedTimer } from '../common';
import { renderToolOutputBody } from './ToolOutputRenderer';
import { ThinkingStepRow } from './ThinkingStepRow';
import { extractCodeFromInput } from '../../utils/codeExecParser';
import { CodeView } from './renderers/CodeView';
import { MySpaceBodyContent } from './renderers/MySpaceRenderer';
import { renderInternetSearchInline } from './renderers/SearchRenderer';
import { coerceOutput, computeEffectiveStatus } from './renderers/utils';
import { t } from '../../i18n';

/**
 * Returns a `{ prefix, value, count }` label descriptor for the header row.
 * `value` is rendered as a subtle chip; omitted when empty.
 */
function getRowLabel(
  tool: ToolCall,
  parsed: unknown,
  displayName: string,
): { prefix: string; value: string; count?: number } {
  if (!tool.output || tool.status === 'running') return { prefix: displayName, value: '' };

  try {
    const out = parsed as any;

    switch (tool.name) {
      case 'internet_search': {
        const sr = out?.result ?? out;
        const rawQuery = String(sr?.query ?? out?.query ?? '').trim();
        const query = rawQuery.length > 60 ? rawQuery.slice(0, 60) + '…' : rawQuery;
        const count = Array.isArray(sr?.results) ? sr.results.length : undefined;
        return { prefix: t('搜索页面：'), value: query || displayName, count };
      }
      case 'retrieve_dataset_content': {
        const items = out?.items;
        return { prefix: t('知识库检索'), value: '', count: Array.isArray(items) ? items.length : undefined };
      }
      case 'retrieve_local_kb': {
        const items = Array.isArray(out) ? out : out?.items;
        return { prefix: t('本地知识库'), value: '', count: Array.isArray(items) ? items.length : undefined };
      }
      case 'get_industry_news':
        return { prefix: t('产业资讯'), value: '', count: Array.isArray(out?.items) ? out.items.length : undefined };
      case 'get_latest_ai_news':
        return { prefix: t('AI 热点资讯'), value: '', count: Array.isArray(out?.items) ? out.items.length : undefined };
      case 'search_company':
        return { prefix: t('企业搜索'), value: '', count: Array.isArray(out?.items) ? out.items.length : undefined };
      case 'list_datasets': {
        const n =
          (Array.isArray(out?.public_datasets) ? out.public_datasets.length : 0) +
          (Array.isArray(out?.private_datasets) ? out.private_datasets.length : 0);
        return { prefix: t('知识库列表'), value: '', count: n };
      }
      case 'load_skill': {
        const sn = (tool.input as any)?.skill_name || (tool.input as any)?.name || '';
        return { prefix: t('激活技能：'), value: sn || '' };
      }
      case 'view_text_file': {
        const fp = (tool.input as any)?.file_name || (tool.input as any)?.path || '';
        const fn = fp ? String(fp).split('/').pop() || '' : '';
        return { prefix: t('读取文件：'), value: fn };
      }
      case 'generate_chart_tool': return { prefix: t('生成图表'), value: '' };
      case 'word_create_from_markdown': return { prefix: t('生成 Word 文档'), value: '' };
      case 'export_report_to_docx': return { prefix: t('导出 Word 报告'), value: '' };
      case 'export_table_to_excel': return { prefix: t('导出 Excel 表格'), value: '' };
      case 'web_fetch': {
        let domain = '';
        try { domain = new URL(out?.url || '').hostname; } catch { /* noop */ }
        return { prefix: t('获取网页：'), value: domain };
      }
      case 'call_subagent': {
        const an = tool.displayName?.split('：')[1]?.trim() || '';
        return { prefix: t('调用智能体：'), value: an };
      }
      case 'list_myspace_files': return { prefix: t('读取我的空间'), value: '' };
      case 'stage_myspace_file': {
        const fn = (tool.input as any)?.file_path?.split('/').pop() || '';
        return { prefix: t('导入文件：'), value: fn };
      }
      case 'list_favorite_chats': return { prefix: t('获取收藏会话'), value: '' };
      case 'get_chat_messages': return { prefix: t('读取会话记录'), value: '' };
      case 'list_team_files': {
        const tid = (tool.input as any)?.team_id || '';
        return { prefix: tid ? t('浏览团队文件：') : t('浏览团队文件夹'), value: tid };
      }
      case 'stage_team_file': {
        const aid = (tool.input as any)?.artifact_id || '';
        return { prefix: t('导入团队文件：'), value: aid };
      }
      case 'get_chain_information': return { prefix: t('产业链分析'), value: '' };
      case 'get_company_base_info': return { prefix: t('企业基本信息'), value: '' };
      case 'get_company_business_analysis': return { prefix: t('企业经营分析'), value: '' };
      case 'get_company_tech_insight': return { prefix: t('企业技术洞察'), value: '' };
      case 'get_company_funding': return { prefix: t('资金穿透分析'), value: '' };
      case 'get_company_risk_warning': return { prefix: t('风险预警'), value: '' };
      case 'query_database': return { prefix: t('数据库查询'), value: '' };
      case 'bash': {
        const cmd = (tool.input as any)?.command || '';
        // Truncate long commands so the chip doesn't blow out the row
        const display = cmd.length > 80 ? cmd.slice(0, 77) + '…' : cmd;
        return { prefix: t('执行命令'), value: display };
      }
      case 'sandbox_put_artifact': {
        const dst = (tool.input as any)?.dest_path || '';
        return { prefix: t('写入沙盒文件'), value: dst };
      }
      case 'sandbox_get_artifact': {
        const src = (tool.input as any)?.src_path || '';
        return { prefix: t('保存沙盒文件'), value: src };
      }
      case 'get_skills': return { prefix: t('获取技能列表'), value: '' };
      case 'get_agents': return { prefix: t('获取智能体列表'), value: '' };
      case 'get_mcp_tools': return { prefix: t('获取 MCP 工具'), value: '' };
      default: return { prefix: displayName, value: '' };
    }
  } catch {
    return { prefix: displayName, value: '' };
  }
}

/**
 * Code-writing tools whose input `extractCodeFromInput` can render as a
 * code/command view. While running the card shows that view (collapsed by
 * default; click to open) so the user can see what is executing. Keep in
 * sync with the tool branches in `utils/codeExecParser.ts`.
 */
const STREAM_CODE_TOOLS = new Set([
  'bash', 'Write', 'Edit',
]);

/** Header label for a code tool while it is running. */
function getRunningCodeLabel(tool: ToolCall): { prefix: string; value: string } {
  const input = (tool.input ?? {}) as Record<string, unknown>;
  const basename = (p: unknown) => String(p ?? '').split('/').pop() || '';
  switch (tool.name) {
    case 'bash':
      return { prefix: t('执行命令'), value: '' };
    case 'Write':
      return { prefix: t('写入文件：'), value: basename(input.file_path) };
    case 'Edit':
      return { prefix: t('编辑文件：'), value: basename(input.file_path) };
    default:
      return { prefix: t('执行命令'), value: '' };
  }
}

/**
 * Completed-step marker. Mirrors the reference design: instead of a generic
 * success tick, each row carries a quiet outline icon that signals *what kind*
 * of step it was (search / browse / file / code / …). Unknown tools fall back
 * to a small hollow dot.
 */
function StepIcon({ name }: { name: string }) {
  const map: Record<string, ComponentType<{ className?: string }>> = {
    internet_search: SearchOutlined,
    get_industry_news: SearchOutlined,
    get_latest_ai_news: SearchOutlined,
    search_company: SearchOutlined,
    web_fetch: GlobalOutlined,
    view_text_file: FileTextOutlined,
    Write: EditOutlined,
    Edit: EditOutlined,
    bash: CodeOutlined,
    retrieve_dataset_content: DatabaseOutlined,
    retrieve_local_kb: DatabaseOutlined,
    list_datasets: DatabaseOutlined,
    query_database: DatabaseOutlined,
    load_skill: ThunderboltOutlined,
    get_skills: ThunderboltOutlined,
    get_mcp_tools: ThunderboltOutlined,
    call_subagent: RobotOutlined,
    get_agents: RobotOutlined,
    list_myspace_files: FolderOutlined,
    stage_myspace_file: FolderOutlined,
    list_team_files: FolderOutlined,
    stage_team_file: FolderOutlined,
    sandbox_put_artifact: FolderOutlined,
    sandbox_get_artifact: FolderOutlined,
    list_favorite_chats: FolderOutlined,
    get_chat_messages: FolderOutlined,
    generate_chart_tool: BarChartOutlined,
    word_create_from_markdown: FileWordOutlined,
    export_report_to_docx: FileWordOutlined,
    export_table_to_excel: FileExcelOutlined,
  };
  const Ico = map[name];
  if (!Ico) return <span className="jx-tcr-dot" aria-hidden="true" />;
  return <Ico className="jx-tcr-icon jx-tcr-icon--step" />;
}

interface ToolCallRowProps {
  tool: ToolCall;
  isStreaming?: boolean;
}

export function ToolCallRow({ tool, isStreaming }: ToolCallRowProps) {
  // Collapsed by default (including while a code tool is running) — only
  // expands when the user explicitly clicks to open it.
  const [expanded, setExpanded] = useState(false);
  const toolDisplayNames = useChatStore((s) => s.toolDisplayNames);
  const setDetailModal = useUIStore((s) => s.setDetailModal);

  const effectiveStatus = computeEffectiveStatus(tool, isStreaming);
  const parsed = useMemo(() => coerceOutput(tool.output), [tool.output]);

  const displayName =
    tool.displayName ||
    TOOL_NAME_OVERRIDES[tool.name] ||
    toolDisplayNames[tool.name] ||
    tool.name;

  const isLiveCode =
    effectiveStatus === 'running' && STREAM_CODE_TOOLS.has(tool.name);

  const liveCode = useMemo(
    () => (isLiveCode ? extractCodeFromInput(tool.name, tool.input) : null),
    [isLiveCode, tool.name, tool.input],
  );

  const { prefix, value, count } = useMemo(
    () => (isLiveCode
      ? { ...getRunningCodeLabel(tool), count: undefined }
      : getRowLabel(tool, parsed, displayName)),
    [isLiveCode, tool, parsed, displayName],
  );

  const running = effectiveStatus === 'running';
  const hasOutput = !!tool.output;
  // Command/code is viewable while running for code tools.
  const liveInputView = isLiveCode && !!liveCode?.code;
  // Sub-agent card: its internal thinking/tool/content sub-steps hang under this card and should be collapsible.
  const hasSubSteps = Array.isArray(tool.subSteps) && tool.subSteps.length > 0;
  const canExpand = (hasOutput && !running) || liveInputView || hasSubSteps;
  const toggle = () => { if (canExpand) setExpanded((v) => !v); };

  // Auto-expand once when sub-steps first appear, so the streaming process is visible by default; afterwards the user can collapse it
  // and it won't be force-expanded again by later sub-steps (auto-opens only once, remembered via a ref).
  const autoOpenedRef = useRef(false);
  useEffect(() => {
    if (hasSubSteps && !autoOpenedRef.current) {
      autoOpenedRef.current = true;
      setExpanded(true);
    }
  }, [hasSubSteps]);

  const renderBody = () => {
    if (!tool.output) return null;
    switch (tool.name) {
      case 'list_myspace_files':
      case 'stage_myspace_file':
      case 'list_favorite_chats':
      case 'get_chat_messages':
        return <MySpaceBodyContent tool={tool} />;
      case 'internet_search':
        return renderInternetSearchInline(parsed);
      default:
        return renderToolOutputBody(tool.name, parsed, setDetailModal);
    }
  };

  // The model buffers tool-call args server-side, so `liveCode.code` arrives
  // fully-formed (not token-by-token) — this is a static view, not a typing
  // animation.
  const expandedBody = liveInputView && liveCode
    ? <CodeView code={liveCode.code} language={liveCode.language} className="jx-tcr-liveCode" />
    : hasOutput ? renderBody() : null;

  return (
    <div className={`jx-tcr${effectiveStatus === 'error' ? ' jx-tcr--error' : ''}`}>
      <div
        className={`jx-tcr-header${expanded ? ' jx-tcr-header--open' : ''}`}
        role={canExpand ? 'button' : undefined}
        tabIndex={canExpand ? 0 : undefined}
        onClick={toggle}
        onKeyDown={(e) => {
          if (canExpand && (e.key === 'Enter' || e.key === ' ')) {
            e.preventDefault();
            toggle();
          }
        }}
      >
        {/* Streaming: key on status remounts the span so running→done settles
            with a scale-in. Non-streaming (history) renders unkeyed + without
            the animation class, so reloads stay static. */}
        <span
          key={isStreaming ? `st-${effectiveStatus}` : 'st'}
          className={`jx-tcr-status${isStreaming ? ' jx-anim-statusIn' : ''}`}
        >
          {effectiveStatus === 'running' && <LoadingOutlined spin className="jx-tcr-icon jx-tcr-icon--running" />}
          {effectiveStatus === 'success' && <StepIcon name={tool.name} />}
          {effectiveStatus === 'error' && <CloseOutlined className="jx-tcr-icon jx-tcr-icon--error" />}
        </span>
        <span className="jx-tcr-label">
          <span className="jx-tcr-prefix">{prefix}</span>
          {value && <span className="jx-tcr-value">{value}</span>}
          {count != null && <span className="jx-tcr-count">&nbsp;({count})</span>}
        </span>
        {running && tool.timestamp ? (
          <ElapsedTimer startTs={tool.timestamp} className="jx-tcr-timer" />
        ) : null}
        {canExpand && (
          <span className={`jx-tcr-arrow${expanded ? ' jx-tcr-arrow--open' : ''}`} />
        )}
      </div>

      {/* Sub-agent internal streaming process: thinking (reuses the "Thinking" module) / tool calls / generated content,
          mounted live under this card; thinking + tool steps are persisted with call_subagent and can be replayed after refresh.
          Collapses with the card header (controlled by expanded), auto-expanded once so the streaming process can be viewed. */}
      {expanded && Array.isArray(tool.subSteps) && tool.subSteps.length > 0 && (
        <div className="jx-tcr-subagent">
          {tool.subSteps.map((s, i) => {
            if (s.kind === 'tool') {
              return (
                <ToolCallRow
                  key={`sub-t-${s.toolId || i}`}
                  tool={{
                    id: s.toolId,
                    name: s.name || 'tool',
                    displayName: s.displayName,
                    input: s.input,
                    output: s.output,
                    status: s.status,
                  }}
                  isStreaming={isStreaming}
                />
              );
            }
            if (s.kind === 'thinking') {
              // Same "Thinking" module as the main agent (collapsible, lightbulb icon). While streaming and on the last step,
              // shows a "Thinking…" spinner.
              return (
                <ThinkingStepRow
                  key={`sub-think-${i}`}
                  content={s.text || ''}
                  active={!!isStreaming && i === tool.subSteps!.length - 1}
                />
              );
            }
            // content: streaming preview of the sub-agent's answer (the final answer is in this card's result)
            return (
              <div key={`sub-x-${i}`} className="jx-tcr-subtext jx-tcr-subtext--content">
                {s.text}
              </div>
            );
          })}
        </div>
      )}

      {(liveInputView || hasOutput) && (
        <div className={`jx-expandWrap${expanded ? ' jx-expandWrap--open' : ''}`}>
          <div className="jx-tcr-body">
            {expanded && expandedBody}
          </div>
        </div>
      )}
    </div>
  );
}
