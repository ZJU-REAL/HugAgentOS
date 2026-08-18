import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { Button, Empty, Input, Select, Tooltip } from 'antd';
import {
  PlusOutlined, LeftOutlined, RightOutlined,
  ClockCircleOutlined, SearchOutlined, ReloadOutlined,
} from '@ant-design/icons';
import { useAutomationStore, useCatalogStore } from '../../stores';
import { usePanelHeader } from '../../hooks/usePageConfig';
import { useDelayedFlag } from '../../hooks';
import { getAutomationRuns } from '../../api';
import type { AutomationRun, AutomationTask } from '../../types';
import { EASE } from '../../utils/motionTokens';
import { formatShortDateTime } from '../../utils/date';
import { AutomationCard } from './AutomationCard';
import { AutomationCreateModal } from './AutomationCreateModal';
import { AutomationDetailPage } from './AutomationDetailPage';
import { AutomationListSkeleton } from './AutomationSkeleton';
import { AUTOMATION_PRESETS, type AutomationPreset } from './automationPresets';
import { cronToHumanReadable, RUN_STATUS_CLASS, RUN_STATUS_LABEL } from './automationUtils';
import '../../styles/automation.css';
import { t } from '../../i18n';

type AutomationTab = 'tasks' | 'runs';
type SortKey = 'created_desc' | 'created_asc' | 'next_run';

/** 聚合执行记录一次最多展示多少条——后端只有「按任务查 runs」的接口，合并后可能很长。 */
const RUNS_DISPLAY_LIMIT = 50;
/** 每个任务取多少条最近执行记录参与合并。 */
const RUNS_PER_TASK = 10;

/** 执行记录列表项：run 本身不带任务名，聚合展示时要把所属任务带上。 */
interface AggregatedRun extends AutomationRun {
  taskName: string;
}

export function AutomationPanel() {
  const {
    tasks,
    loading,
    createModalOpen,
    selectedTaskId,
    fetchTasks,
    setCreateModalOpen,
    setSelectedTaskId,
  } = useAutomationStore();
  const panel = useCatalogStore((s) => s.panel);
  const panelEntryNonce = useCatalogStore((s) => s.panelEntryNonce);

  const { title, subtitle } = usePanelHeader('automation', {
    title: '定时任务',
    subtitle: '按计划自动执行任务，也可随时手动触发。在任意对话中描述你想定期做的事，即可快速创建',
  });

  const [tab, setTab] = useState<AutomationTab>('tasks');
  const [keyword, setKeyword] = useState('');
  const [sortKey, setSortKey] = useState<SortKey>('created_desc');
  const [pendingPreset, setPendingPreset] = useState<AutomationPreset | null>(null);

  useEffect(() => {
    void fetchTasks();
  }, [fetchTasks]);

  // 重新进入本面板时回到列表——与 AgentPanel / SkillsPage / McpPage 用的是同一套
  // `panelEntryNonce` 约定，这样侧边栏（以及任何别的导航入口）不必知道本功能的存在。
  useEffect(() => {
    if (panel !== 'automation') return;
    setSelectedTaskId(null);
  }, [panel, panelEntryNonce, setSelectedTaskId]);

  const showListSkeleton = useDelayedFlag(loading && tasks.length === 0);

  const openCreate = useCallback((preset: AutomationPreset | null) => {
    setPendingPreset(preset);
    setCreateModalOpen(true);
  }, [setCreateModalOpen]);

  // 过滤 + 排序 + 分组一次算完：三个状态桶此前是三次独立 filter，每次按键都全量重扫。
  const { active: activeTasks, paused: pausedTasks, other: otherTasks, total } = useMemo(() => {
    const q = keyword.trim().toLowerCase();
    const filtered = q
      ? tasks.filter((task) => (
        (task.name || '').toLowerCase().includes(q)
        || (task.description || '').toLowerCase().includes(q)
        || (task.prompt || '').toLowerCase().includes(q)
      ))
      : tasks;
    const sorted = [...filtered].sort((a, b) => {
      if (sortKey === 'created_asc') return (a.created_at || '').localeCompare(b.created_at || '');
      if (sortKey === 'next_run') {
        // 没有下次执行时间（手动/已停用）的排在最后
        if (!a.next_run_at) return b.next_run_at ? 1 : 0;
        if (!b.next_run_at) return -1;
        return a.next_run_at.localeCompare(b.next_run_at);
      }
      return (b.created_at || '').localeCompare(a.created_at || '');
    });
    return {
      active: sorted.filter((task) => task.status === 'active'),
      paused: sorted.filter((task) => task.status === 'paused'),
      other: sorted.filter((task) => !['active', 'paused'].includes(task.status)),
      total: sorted.length,
    };
  }, [tasks, keyword, sortKey]);

  // ── 详情页（替换整页，保持原有行为） ──
  if (selectedTaskId) {
    return (
      <AutomationDetailPage
        taskId={selectedTaskId}
        onBack={() => {
          setSelectedTaskId(null);
          void fetchTasks();
        }}
      />
    );
  }

  // 任务卡片增删动画：popLayout + layout，key=task_id；轮询整组替换时稳定 key 避免重挂载。
  const renderTaskCards = (list: AutomationTask[]) => (
    <AnimatePresence mode="popLayout" initial={false}>
      {list.map((task) => (
        <motion.div
          key={task.task_id}
          layout
          initial={{ opacity: 0, scale: 0.98 }}
          animate={{ opacity: 1, scale: 1 }}
          exit={{ opacity: 0, scale: 0.96, transition: { duration: 0.2, ease: EASE.standard } }}
          transition={{ duration: 0.2, ease: EASE.standard }}
        >
          <AutomationCard task={task} onClick={() => setSelectedTaskId(task.task_id)} />
        </motion.div>
      ))}
    </AnimatePresence>
  );

  const renderTasksTab = () => {
    if (showListSkeleton) return <AutomationListSkeleton />;
    if (loading && tasks.length === 0) return null;
    if (total === 0) {
      const searching = !!keyword.trim();
      return (
        <div className="jx-automation-empty">
          <Empty
            image={<ClockCircleOutlined style={{ fontSize: 44, opacity: 0.3 }} />}
            description={(
              <>
                <div className="jx-automation-emptyTitle">
                  {searching ? t('没有匹配的定时任务') : t('暂无定时任务')}
                </div>
                <div className="jx-automation-emptyDesc">
                  {searching ? t('换个关键词试试') : t('创建定时任务来自动执行周期性 AI 工作')}
                </div>
              </>
            )}
          >
            {!searching && (
              <Button icon={<PlusOutlined />} onClick={() => openCreate(null)}>
                {t('新建定时任务')}
              </Button>
            )}
          </Empty>
        </div>
      );
    }
    return (
      <>
        {activeTasks.length > 0 && (
          <div className="jx-automation-section">
            <div className="jx-automation-sectionTitle">{t('运行中')}</div>
            {renderTaskCards(activeTasks)}
          </div>
        )}
        {pausedTasks.length > 0 && (
          <div className="jx-automation-section">
            <div className="jx-automation-sectionTitle">{t('已暂停')}</div>
            {renderTaskCards(pausedTasks)}
          </div>
        )}
        {otherTasks.length > 0 && (
          <div className="jx-automation-section">
            <div className="jx-automation-sectionTitle">{t('已完成 / 已停用')}</div>
            {renderTaskCards(otherTasks)}
          </div>
        )}
      </>
    );
  };

  return (
    // jx-automationPage 只是移动端样式的作用域锚点：页头「搜索 + 新建」在窄屏要竖排，
    // 而 .jx-agentPage-header 是能力中心等页共用的通用类，不能直接改。
    <div className="jx-agentPage jx-automationPage">
      <div className="jx-agentPage-header">
        <div>
          <div className="jx-agentPage-title">{title}</div>
          {subtitle ? <div className="jx-agentPage-subtitle">{subtitle}</div> : null}
        </div>
        <div className="jx-automation-headerRight">
          <Input
            allowClear
            className="jx-automation-search"
            prefix={<SearchOutlined style={{ color: 'var(--color-text-placeholder)' }} />}
            placeholder={t('搜索定时任务…')}
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
          />
          <Button type="primary" icon={<PlusOutlined />} onClick={() => openCreate(null)}>
            {t('新建定时任务')}
          </Button>
        </div>
      </div>

      <div className="jx-automation-body">
        <PresetCarousel onPick={openCreate} />

        <div className="jx-automation-listHeader">
          <div className="jx-automation-tabs" role="tablist" aria-label={t('定时任务视图')}>
            {([
              { key: 'tasks' as const, label: t('我的定时任务') },
              { key: 'runs' as const, label: t('执行记录') },
            ]).map((item) => (
              <button
                key={item.key}
                type="button"
                role="tab"
                aria-selected={tab === item.key}
                className={`jx-automation-tab${tab === item.key ? ' active' : ''}`}
                onClick={() => setTab(item.key)}
              >
                {item.label}
              </button>
            ))}
          </div>
          {tab === 'tasks' && (
            <Select
              size="small"
              className="jx-automation-sort"
              value={sortKey}
              onChange={setSortKey}
              options={[
                { value: 'created_desc', label: t('按创建时间倒序') },
                { value: 'created_asc', label: t('按创建时间正序') },
                { value: 'next_run', label: t('按下次执行时间') },
              ]}
            />
          )}
        </div>

        {/* 两个 Tab 都常驻挂载、用 CSS 显隐切换：来回切 Tab 不该把已经拉好的执行记录丢掉重拉。 */}
        <div hidden={tab !== 'tasks'}>{renderTasksTab()}</div>
        <div hidden={tab !== 'runs'}>
          <RunsTab tasks={tasks} onOpenTask={setSelectedTaskId} />
        </div>
      </div>

      <AutomationCreateModal
        open={createModalOpen}
        preset={pendingPreset
          ? { name: pendingPreset.title, prompt: pendingPreset.prompt, cron: pendingPreset.cron }
          : null}
        onClose={() => {
          setCreateModalOpen(false);
          setPendingPreset(null);
        }}
        onCreated={() => {
          setCreateModalOpen(false);
          setPendingPreset(null);
          void fetchTasks();
        }}
      />
    </div>
  );
}

/** 推荐任务：横向卡片轮播，点卡片把示例预填进创建弹窗。 */
function PresetCarousel({ onPick }: { onPick: (preset: AutomationPreset) => void }) {
  const trackRef = useRef<HTMLDivElement | null>(null);
  const rafRef = useRef<number | null>(null);
  const [edges, setEdges] = useState({ atStart: true, atEnd: false });

  // 平滑滚动期间 scroll 事件按帧率连发，每次都读 scrollLeft/clientWidth/scrollWidth 会强制
  // 同步布局。合并到一个 rAF 里，一次滚动动画只测量若干次、且只有真正翻面时才 setState。
  const syncEdges = useCallback(() => {
    if (rafRef.current !== null) return;
    rafRef.current = window.requestAnimationFrame(() => {
      rafRef.current = null;
      const el = trackRef.current;
      if (!el) return;
      const next = {
        atStart: el.scrollLeft <= 1,
        atEnd: el.scrollLeft + el.clientWidth >= el.scrollWidth - 1,
      };
      setEdges((prev) => (
        prev.atStart === next.atStart && prev.atEnd === next.atEnd ? prev : next
      ));
    });
  }, []);

  useEffect(() => {
    syncEdges();
    window.addEventListener('resize', syncEdges);
    return () => {
      window.removeEventListener('resize', syncEdges);
      if (rafRef.current !== null) window.cancelAnimationFrame(rafRef.current);
    };
  }, [syncEdges]);

  const scrollBy = (dir: -1 | 1) => {
    const el = trackRef.current;
    if (!el) return;
    el.scrollBy({ left: dir * Math.max(el.clientWidth - 80, 240), behavior: 'smooth' });
  };

  return (
    <section className="jx-automation-presets">
      <div className="jx-automation-presetsHead">
        <div className="jx-automation-presetsTitle">{t('推荐任务')}</div>
        <div className="jx-automation-presetsNav">
          <button
            type="button"
            className="jx-automation-presetsArrow"
            aria-label={t('上一组')}
            disabled={edges.atStart}
            onClick={() => scrollBy(-1)}
          >
            <LeftOutlined />
          </button>
          <button
            type="button"
            className="jx-automation-presetsArrow"
            aria-label={t('下一组')}
            disabled={edges.atEnd}
            onClick={() => scrollBy(1)}
          >
            <RightOutlined />
          </button>
        </div>
      </div>
      <div className="jx-automation-presetsTrack" ref={trackRef} onScroll={syncEdges}>
        {AUTOMATION_PRESETS.map((preset) => (
          <button
            type="button"
            key={preset.id}
            className="jx-automation-presetCard jx-card-lift"
            onClick={() => onPick(preset)}
          >
            <div className="jx-automation-presetCard-title">{preset.title}</div>
            <div className="jx-automation-presetCard-desc">{preset.prompt}</div>
            <div className="jx-automation-presetCard-cron">
              <ClockCircleOutlined />
              <span>{cronToHumanReadable(preset.cron)}</span>
            </div>
          </button>
        ))}
      </div>
    </section>
  );
}

/**
 * 执行记录：后端只有「按任务查 runs」的接口，这里按任务并发拉取后在前端合并排序。
 *
 * 因此这是个 N 次请求的聚合，代价随任务数线性增长，展示上也只能是「每个任务最近 N 条」的并集
 * 而非真正的全局最近 N 条。任务量再大就该在后端加一个 `GET /v1/automations/runs` 聚合接口
 * （runs join tasks，按 started_at 倒序 limit），这里改成一次请求。
 */
function RunsTab({ tasks, onOpenTask }: { tasks: AutomationTask[]; onOpenTask: (id: string) => void }) {
  const [runs, setRuns] = useState<AggregatedRun[]>([]);
  const [loading, setLoading] = useState(false);
  // 依赖任务**集合**而非数组引用：fetchTasks 每次都 set 一个新数组，直接依赖 tasks
  // 会让轮询/刷新触发一整轮 N 次重复请求。
  const taskSig = useMemo(() => tasks.map((task) => task.task_id).join('|'), [tasks]);
  const tasksRef = useRef(tasks);
  tasksRef.current = tasks;

  const reload = useCallback(async () => {
    const list = tasksRef.current;
    if (list.length === 0) {
      setRuns([]);
      return;
    }
    setLoading(true);
    try {
      const results = await Promise.all(
        list.map(async (task) => {
          try {
            const items = await getAutomationRuns(task.task_id, RUNS_PER_TASK);
            return items.map((run) => ({ ...run, taskName: task.name || t('未命名任务') }));
          } catch {
            // 单个任务拉取失败不影响整体列表
            return [] as AggregatedRun[];
          }
        }),
      );
      const merged = results.flat();
      merged.sort((a, b) => (b.started_at || '').localeCompare(a.started_at || ''));
      setRuns(merged.slice(0, RUNS_DISPLAY_LIMIT));
    } finally {
      setLoading(false);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskSig]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const showSkeleton = useDelayedFlag(loading && runs.length === 0);

  if (showSkeleton) return <AutomationListSkeleton count={3} />;
  if (loading && runs.length === 0) return null;

  if (runs.length === 0) {
    return (
      <div className="jx-automation-empty">
        <Empty
          image={<ClockCircleOutlined style={{ fontSize: 44, opacity: 0.3 }} />}
          description={(
            <>
              <div className="jx-automation-emptyTitle">{t('暂无执行记录')}</div>
              <div className="jx-automation-emptyDesc">{t('定时任务执行后，这里会汇总每一次的运行结果')}</div>
            </>
          )}
        />
      </div>
    );
  }

  return (
    <div>
      <div className="jx-automation-runListHead">
        <span className="jx-automation-runListCount">{t('共 {n} 条', { n: runs.length })}</span>
        <Tooltip title={t('刷新')}>
          <Button size="small" type="text" icon={<ReloadOutlined />} loading={loading} onClick={() => void reload()} />
        </Tooltip>
      </div>
      {runs.map((run) => (
        <div
          key={run.run_id}
          className="jx-automation-runRow jx-card-lift"
          role="button"
          tabIndex={0}
          onClick={() => onOpenTask(run.task_id)}
        >
          <span className={`jx-automation-runDot ${RUN_STATUS_CLASS[run.status] || 'is-failed'}`} />
          <div className="jx-automation-runMain">
            <div className="jx-automation-runTitle">
              <span className="jx-automation-runTaskName">{run.taskName}</span>
              <span className="jx-automation-runStatus">{RUN_STATUS_LABEL[run.status] || run.status}</span>
            </div>
            {run.result_summary && (
              <div className="jx-automation-runSummary">{run.result_summary}</div>
            )}
            {run.error_message && (
              <div className="jx-automation-runError">{run.error_message}</div>
            )}
          </div>
          <div className="jx-automation-runMeta">
            <span>{formatShortDateTime(run.started_at, '—')}</span>
            {typeof run.duration_ms === 'number' && (
              <span className="jx-automation-runDuration">{t('{n} 秒', { n: Math.round(run.duration_ms / 1000) })}</span>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
