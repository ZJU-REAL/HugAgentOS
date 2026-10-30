import { useMemo, useState, type ReactNode } from 'react';
import { CaretDownFilled, CloudOutlined, CodeOutlined, FolderOutlined, LaptopOutlined, LoadingOutlined, SettingOutlined, UnorderedListOutlined } from '@ant-design/icons';
import { message, Tooltip } from 'antd';
import { t } from '../../i18n';
import { useChatStore } from '../../stores/chatStore';
import { fileTabId, subagentTabId, useCanvasStore, type CanvasArtifact } from '../../stores/canvasStore';
import { useSubagentIdentity } from '../../hooks/useSubagentIdentity';
import { subagentStatus } from '../../utils/subagentView';
import { formatClock } from '../../utils/elapsed';
import { formatTokens } from '../../utils/contextUsage';
import { parseServerTime } from '../../utils/date';
import type { CommandTaskBrief, JobBrief } from '../../types';
import { AgentIcon } from '../agent/AgentIcon';
import { ElapsedTimer } from '../common';
import { CONTEXT_CATEGORIES, useContextUsageView } from '../chat/useContextUsageView';
import { DeliveryFileIcon } from '../chat/DeliveryFileIcon';
import { SubagentStatus } from '../tool/SubagentCallRow';
import { openSubagentTab } from '../tool/openSubagentTab';
import { collectFiles, collectSubagents, countToolCalls, type MonitorSubagent } from './runMonitorData';
import { isLiveCommand, isLiveJob, useBackgroundTasks } from './useBackgroundTasks';
import { useChatProject } from './useChatProject';
import ProjectEnvironmentModal from '../projects/ProjectEnvironmentModal';
import { projectSpaceLocation } from '../projects/projectFolderLocation';
import { useMySpaceStore } from '../../stores/mySpaceStore';
// DeliveryFileIcon takes its per-type colours from the delivery card styles.
import '../chat/DeliveredFiles.css';
import './RunMonitorRail.css';

interface MonitorCardProps {
  title: string;
  /** Right side of the header: a count, a percentage, a short state. */
  summary?: ReactNode;
  defaultOpen?: boolean;
  children: ReactNode;
}

function MonitorCard({ title, summary, defaultOpen = true, children }: MonitorCardProps) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className="jx-runMonitor-card" data-open={open || undefined}>
      <button type="button" className="jx-runMonitor-cardHead" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="jx-runMonitor-cardTitle">{title}</span>
        <CaretDownFilled className="jx-runMonitor-caret" aria-hidden />
        {summary != null && <span className="jx-runMonitor-summary">{summary}</span>}
      </button>
      {open && <div className="jx-runMonitor-cardBody">{children}</div>}
    </section>
  );
}

function SubagentRow({ item, chatId, active }: { item: MonitorSubagent; chatId: string; active: boolean }) {
  const { tool, messageUid, isStreaming } = item;
  const agent = useSubagentIdentity(tool);
  const status = subagentStatus(tool, isStreaming);
  return (
    <button
      type="button"
      className="jx-runMonitor-row"
      data-active={active || undefined}
      onClick={() => openSubagentTab(tool, agent, { chatId, messageUid })}
      aria-label={t('查看智能体「{name}」的执行过程', { name: agent.name })}
    >
      <span className="jx-runMonitor-lead"><AgentIcon agent={agent} size={20} /></span>
      <span className="jx-runMonitor-name">{agent.name}</span>
      {status === 'running' && tool.timestamp
        ? <ElapsedTimer startTs={tool.timestamp} className="jx-runMonitor-meta" />
        : (tool.durationMs ?? 0) >= 1000 && <span className="jx-runMonitor-meta">{formatClock(tool.durationMs! / 1000)}</span>}
      {/* Finished runs stay quiet: only running, failed or interrupted get a mark. */}
      {status !== 'success' && (
        <span className="jx-runMonitor-status" data-status={status}>
          <SubagentStatus tool={tool} isStreaming={isStreaming} />
        </span>
      )}
    </button>
  );
}

function FileRow({ file, active }: { file: CanvasArtifact; active: boolean }) {
  const openCanvas = useCanvasStore((s) => s.openCanvas);
  const extension = file.name.includes('.') ? file.name.split('.').pop()?.toUpperCase() : '';
  return (
    <button
      type="button"
      className="jx-runMonitor-row"
      data-active={active || undefined}
      title={file.name}
      onClick={() => openCanvas(file)}
      aria-label={t('预览文件：{name}', { name: file.name })}
    >
      <span className="jx-runMonitor-lead"><DeliveryFileIcon name={file.name} size={20} /></span>
      <span className="jx-runMonitor-name">{file.name}</span>
      {extension && <span className="jx-runMonitor-meta">{extension}</span>}
    </button>
  );
}

function StopButton({ label, onStop }: { label: string; onStop: () => Promise<void> }) {
  const [stopping, setStopping] = useState(false);
  const stop = async () => {
    setStopping(true);
    try {
      await onStop();
    } catch (error) {
      message.error(t('停止失败：{reason}', { reason: error instanceof Error ? error.message : String(error) }));
    } finally {
      setStopping(false);
    }
  };
  return (
    <Tooltip title={t('停止')}>
      <button type="button" className="jx-runMonitor-stop" aria-label={label} disabled={stopping} onClick={() => void stop()}>
        {stopping ? <LoadingOutlined /> : <span className="jx-runMonitor-stopGlyph" aria-hidden />}
      </button>
    </Tooltip>
  );
}

function CommandRow({ task, onStop }: { task: CommandTaskBrief; onStop: () => Promise<void> }) {
  const started = parseServerTime(task.created_at);
  return (
    <div className="jx-runMonitor-row" title={task.command}>
      <span className="jx-runMonitor-lead jx-runMonitor-glyph"><CodeOutlined /></span>
      <span className="jx-runMonitor-name jx-runMonitor-code">{task.command}</span>
      {Number.isFinite(started) && <ElapsedTimer startTs={started} className="jx-runMonitor-meta" />}
      <StopButton label={t('停止后台命令')} onStop={onStop} />
    </div>
  );
}

function JobRow({ job, onStop }: { job: JobBrief; onStop: () => Promise<void> }) {
  const { settled, total } = job.stats;
  return (
    <div className="jx-runMonitor-row" title={job.name}>
      <span className="jx-runMonitor-lead jx-runMonitor-glyph"><UnorderedListOutlined /></span>
      <span className="jx-runMonitor-name">{job.name}</span>
      {total > 0 && <span className="jx-runMonitor-meta">{settled}/{total}</span>}
      <StopButton label={t('停止批量作业')} onStop={onStop} />
    </div>
  );
}

function BackgroundTasksCard({ chatId }: { chatId: string }) {
  const { commands, jobs, cancelCommand, cancelJob } = useBackgroundTasks(chatId);
  // Only what is still running: the card leaves once every task has ended.
  const liveCommands = commands.filter(isLiveCommand);
  const liveJobs = jobs.filter(isLiveJob);
  const running = liveCommands.length + liveJobs.length;
  if (!running) return null;
  return (
    <MonitorCard title={t('后台任务')} summary={t('{n} 运行中', { n: running })}>
      {liveJobs.map((job) => <JobRow key={job.job_id} job={job} onStop={() => cancelJob(job.job_id)} />)}
      {liveCommands.map((task) => <CommandRow key={task.task_id} task={task} onStop={() => cancelCommand(task.task_id)} />)}
    </MonitorCard>
  );
}

function ContextCard({ toolCalls, toolFailed }: { toolCalls: number; toolFailed: number }) {
  const view = useContextUsageView();
  if (!view) return null;
  const { breakdown, window, pct, level } = view;
  return (
    <MonitorCard
      title={t('上下文')}
      defaultOpen={false}
      summary={<>
        <span className="jx-runMonitor-num">{pct}%</span>
        <span className="jx-runMonitor-mini" data-level={level} aria-hidden>
          <span style={{ width: `${Math.min(100, Math.max(pct, 2))}%` }} />
        </span>
      </>}
    >
      <div className="jx-runMonitor-kv">
        {CONTEXT_CATEGORIES.map((c) => {
          const value = breakdown[c.key] as number;
          if (!(value > 0)) return null;
          return (
            <div key={c.key} className="jx-runMonitor-kvRow">
              <span className="jx-runMonitor-dot" style={{ background: c.color }} />
              <span className="jx-runMonitor-kvLabel">{t(c.label)}</span>
              <span className="jx-runMonitor-num">{formatTokens(value)}</span>
            </div>
          );
        })}
        <div className="jx-runMonitor-kvRow jx-runMonitor-kvTotal">
          <span className="jx-runMonitor-kvLabel">{t('合计')}</span>
          <span className="jx-runMonitor-num">{formatTokens(breakdown.total)} / {formatTokens(window)}</span>
        </div>
        <div className="jx-runMonitor-kvRow">
          <span className="jx-runMonitor-kvLabel">{t('工具调用')}</span>
          <span className="jx-runMonitor-num">
            {toolCalls}
            {toolFailed > 0 && <span className="jx-runMonitor-failed"> · {t('失败 {n}', { n: toolFailed })}</span>}
          </span>
        </div>
      </div>
    </MonitorCard>
  );
}

const pathParts = (path: string) => path.split(/[/\\]/).filter(Boolean);

/**
 * The chat's project: its environment binding and its folder. A local project's
 * folder opens in Finder / Explorer; a cloud project's folder opens in My Space.
 */
function EnvironmentCard({ projectId }: { projectId: string }) {
  const info = useChatProject(projectId);
  const [editing, setEditing] = useState(false);
  const [opening, setOpening] = useState(false);
  if (!info) return null;
  const { project, environment, reloadEnvironment } = info;
  const local = project.kind === 'local';
  const canConfigure = project.permission === 'admin';
  const localPath = local ? project.local_path || '' : '';
  const localParts = pathParts(localPath);
  const folderName = local ? localParts[localParts.length - 1] || project.name : project.folder_name || project.name;
  const teamName = 'team_name' in project && typeof project.team_name === 'string' ? project.team_name : '';
  const folderPlace = local ? localParts[localParts.length - 2] || '' : teamName || t('我的空间');
  const environmentLabel = environment === 'custom' ? t('自定义项目环境') : environment === 'all' ? t('全部可用环境') : t('项目环境');
  const EnvironmentIcon = local ? LaptopOutlined : CloudOutlined;

  const openFolder = async () => {
    if (local) {
      if (localPath) window.location.href = '/__desktop/open-path?path=' + encodeURIComponent(localPath);
      return;
    }
    setOpening(true);
    try {
      const { space, path } = await projectSpaceLocation(project);
      useMySpaceStore.getState().openSpaceFolder(space, path);
    } catch (error) {
      message.error(t('无法打开项目文件夹：{reason}', { reason: error instanceof Error ? error.message : String(error) }));
    } finally {
      setOpening(false);
    }
  };

  return (
    <MonitorCard title={t('环境')}>
      {/* Only the gear opens the settings; the row itself just reports the binding. */}
      <div className="jx-runMonitor-row">
        <span className="jx-runMonitor-lead jx-runMonitor-glyph"><EnvironmentIcon /></span>
        <span className="jx-runMonitor-name">{environmentLabel}</span>
        {canConfigure && (
          <Tooltip title={t('项目环境设置')}>
            <button type="button" className="jx-runMonitor-stop jx-runMonitor-gear" aria-label={t('项目环境设置')} onClick={() => setEditing(true)}>
              <SettingOutlined />
            </button>
          </Tooltip>
        )}
      </div>
      {(!local || localPath) && (
        <button
          type="button"
          className="jx-runMonitor-row"
          disabled={opening}
          onClick={() => void openFolder()}
          title={local ? t('在访达 / 文件管理器中打开项目文件夹：{path}', { path: localPath }) : t('在我的空间中打开项目文件夹')}
        >
          <span className="jx-runMonitor-lead jx-runMonitor-glyph">{opening ? <LoadingOutlined /> : <FolderOutlined />}</span>
          <span className="jx-runMonitor-name">{folderName}</span>
          {folderPlace && <span className="jx-runMonitor-meta jx-runMonitor-place">{folderPlace}</span>}
        </button>
      )}
      {editing && (
        <ProjectEnvironmentModal projectId={projectId} open onClose={() => { setEditing(false); reloadEnvironment(); }} />
      )}
    </MonitorCard>
  );
}

/**
 * Run monitor inside the conversation pane, right of the messages: one card per
 * concern (project environment, context, sub-agents, output and input files,
 * background tasks).
 * Cards with nothing to show stay hidden. Rows open their sub-agent session or
 * file in the canvas; the rail hides when the pane is too narrow for both.
 */
export function RunMonitorRail() {
  const chatId = useChatStore((s) => s.currentChatId);
  const projectId = useChatStore((s) => (s.currentChatId ? s.store.chats[s.currentChatId]?.projectId : undefined));
  const messages = useChatStore((s) => (s.currentChatId ? s.store.chats[s.currentChatId]?.messages : undefined));
  const activeTabId = useCanvasStore((s) => (s.isOpen ? s.activeTabId : null));

  const subagents = useMemo(() => collectSubagents(messages), [messages]);
  const files = useMemo(() => collectFiles(messages, chatId || undefined), [messages, chatId]);
  const toolStats = useMemo(() => countToolCalls(messages), [messages]);

  if (!chatId || !messages?.length) return null;

  return (
    <aside className="jx-runMonitor" aria-label={t('运行监控')}>
      {projectId && <EnvironmentCard key={projectId} projectId={projectId} />}

      <ContextCard toolCalls={toolStats.calls} toolFailed={toolStats.failed} />

      {subagents.length > 0 && (
        <MonitorCard title={t('子智能体')}>
          {subagents.map((item) => (
            <SubagentRow
              key={`${item.messageUid}:${item.tool.id}`}
              item={item}
              chatId={chatId}
              active={activeTabId === subagentTabId({ chatId, messageUid: item.messageUid, toolId: item.tool.id! })}
            />
          ))}
        </MonitorCard>
      )}

      {files.output.length > 0 && (
        <MonitorCard title={t('产出文件')}>
          {[...files.output].reverse().map((file) => (
            <FileRow key={file.file_id} file={file} active={activeTabId === fileTabId(file)} />
          ))}
        </MonitorCard>
      )}

      {files.input.length > 0 && (
        <MonitorCard title={t('输入文件')}>
          {files.input.map((file) => (
            <FileRow key={file.file_id} file={file} active={activeTabId === fileTabId(file)} />
          ))}
        </MonitorCard>
      )}

      <BackgroundTasksCard chatId={chatId} />
    </aside>
  );
}
