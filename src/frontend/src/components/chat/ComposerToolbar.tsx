import { Dropdown } from 'antd';
import { AnimatePresence, motion } from 'motion/react';
import {
  FileImageOutlined, FileTextOutlined, CloudDownloadOutlined,
  AppstoreOutlined, FolderOutlined, FolderOpenOutlined, FolderAddOutlined, RobotOutlined,
  OrderedListOutlined, ThunderboltOutlined, ApiOutlined, SyncOutlined, PartitionOutlined,
  LaptopOutlined, LinkOutlined,
} from '@ant-design/icons';
import { DUR } from '../../utils/motionTokens';
import { useProjectStore } from '../../stores/projectStore';
import { MySpaceImportModal } from '../file';
import CreateProjectModal from '../projects/CreateProjectModal';
import { AgentIcon } from '../agent/AgentIcon';
import { SkillAvatar } from '../catalog/skillIcons';
import { McpIcon } from '../catalog/McpIcon';
import { PluginAvatar } from '../catalog/PluginIconPicker';
import { ContentErrorBoundary } from '../common';
import { ChipChevron } from '../common/ChipChevron';
import { IconPlus } from '../common/DshIcons';
import ApprovalPill from './ApprovalPill';
import ModelEffortChip from './ModelEffortChip';
import { ContextGauge } from './ContextGauge';
import { ComposerModeChip } from './ComposerModeChip';
import { t } from '../../i18n';
import type { InputAreaProps, ComposerOptions } from './composerTypes';
import type { ComposerState } from './useComposerState';
import type { ComposerEditor } from './useComposerEditor';

export function ComposerToolbar({ state, editor, options, fileInputRef, showStopButton, composerHasContent }: {
  state: ComposerState;
  editor: ComposerEditor;
  options: ComposerOptions;
  fileInputRef: InputAreaProps['fileInputRef'];
  showStopButton: boolean;
  composerHasContent: boolean;
}) {
  const { projectComposer, activeMode, disableMention, abort } = options;
  const {
    isModeOn, isAppAllowed, onEnterMode, canReadImage, imageInputRef, showLoopEntry,
    loopMode, setLoopMode, agents, skills, connectors, installedPlugins, activeLocalMode,
    setMySpaceImportOpen, setAttachOpen, fetchAgents, attachOpen, mySpaceImportOpen,
    bindChatProject, currentChatId, isCE, promptHubOpen, setPromptHubOpen,
    boundProjectId, unbindChatProject, projects, onPickProject, isDesktopShell,
    canCreateCloudProject, canCreateLocalProject, setProjectCreateModalOpen,
    setProjectOpen, fetchProjects, projectOpen, boundProjectName,
    planMode, onCloseMode, batchModeOn, workflowModeOn, uploadingFiles,
  } = state;
  const {
    onPickAgentFromMenu, onPickSkillFromMenu, onPickConnectorFromMenu,
    onPickPluginFromMenu, sendFromComposer, forkPending,
  } = editor;
  return (
        <div className="jx-composerBar">
          {/* ➕：附件与能力入口，坐在工具条最左（参考稿把 add 放在左下角，
              和右下角的发送形成一对，输入区两端各一个圆钮）*/}
          {(() => {
            // Mode entries (plan / batch), shared by the main menu and the project-page
            // projectComposer, each gated by allowed_apps. A chat can retain historical plan
            // cards after the user returns to ordinary conversation, so the main menu must use
            // the active composer mode rather than the persistent planChat classification.
            // The menu only turns a mode **on** and marks the one already running; turning it
            // off is the job of the ✕ on the mode chip down in the composer bar.
            const planActive = isModeOn('plan');
            const batchActive = isModeOn('batch');
            const workflowActive = isModeOn('workflow');
            const activeSuffix = projectComposer ? t('（已选）') : t('（已开启）');
            const modeItems = [
              ...(isAppAllowed('plan_mode') ? [{
                key: 'mode-plan',
                icon: <OrderedListOutlined />,
                label: planActive ? t('计划模式{suffix}', { suffix: activeSuffix }) : t('计划模式'),
                onClick: () => onEnterMode('plan'),
              }] : []),
              ...(isAppAllowed('batch_runner') ? [{
                key: 'mode-batch',
                icon: <ThunderboltOutlined />,
                label: batchActive ? t('批量执行{suffix}', { suffix: activeSuffix }) : t('批量执行'),
                onClick: () => onEnterMode('batch'),
              }] : []),
              // 工作流模式：面对成百上千个同构工作项时，让智能体写一段作业脚本交后台并发跑。
              // 与计划模式/批量执行一样是**用户显式触发**的模式——不进入就不注册 run_job、
              // 不注入批量提示词，普通问答完全不受影响。
              {
                key: 'mode-workflow',
                icon: <PartitionOutlined />,
                label: workflowActive ? t('工作流模式{suffix}', { suffix: activeSuffix }) : t('工作流模式'),
                onClick: () => onEnterMode('workflow'),
              },
            ];
            const items = [
              {
                key: 'image',
                icon: <FileImageOutlined />,
                // 没有任何模型能读图时把话说在前面：图还是能传（当附件留档、换模型后仍可用），
                // 但别让用户以为这一轮模型看得见。
                label: canReadImage ? t('上传图片') : t('上传图片（当前模型不识图）'),
                onClick: () => imageInputRef.current?.click(),
              },
              { key: 'file', icon: <FileTextOutlined />, label: t('上传文件'), onClick: () => fileInputRef.current?.click() },
              { type: 'divider' as const },
              ...modeItems,
              ...(showLoopEntry ? [{
                key: 'mode-loop',
                icon: <SyncOutlined />,
                label: loopMode ? t('自主循环{suffix}', { suffix: activeSuffix }) : t('自主循环'),
                onClick: () => setLoopMode(true),
              }] : []),
              ...((modeItems.length > 0 || showLoopEntry) ? [{ type: 'divider' as const }] : []),
              ...(!disableMention ? [{
                key: 'agents',
                icon: <RobotOutlined />,
                label: t('@智能体'),
                children: (() => {
                  const callable = agents || [];
                  if (callable.length === 0) {
                    return [{ key: 'agents-empty', label: t('暂无可用智能体'), disabled: true }];
                  }
                  return callable.map((a) => ({
                    key: `agent-${a.agent_id}`,
                    icon: <AgentIcon agent={a} size={20} />,
                    label: a.name,
                    onClick: () => onPickAgentFromMenu(a),
                  }));
                })(),
              }] : []),
              {
                key: 'skills',
                icon: <AppstoreOutlined />,
                label: t('技能'),
                children: (() => {
                  const callable = skills || [];
                  if (callable.length === 0) {
                    return [{ key: 'skills-empty', label: t('暂无可用技能'), disabled: true }];
                  }
                  return callable.map((s) => ({
                    key: `skill-${s.id}`,
                    icon: <SkillAvatar icon={s.icon} name={s.name} seed={s.id} size={20} round />,
                    label: s.name,
                    onClick: () => onPickSkillFromMenu(s.id, s.name),
                  }));
                })(),
              },
              {
                key: 'connectors',
                icon: <LinkOutlined />,
                label: t('连接器'),
                children: (() => {
                  const callable = connectors || [];
                  if (callable.length === 0) {
                    return [{ key: 'connectors-empty', label: t('暂无可用连接器'), disabled: true }];
                  }
                  return callable.map((c) => ({
                    key: `connector-${c.id}`,
                    icon: <McpIcon id={c.id} icon={c.icon} size={20} />,
                    label: c.name,
                    onClick: () => onPickConnectorFromMenu(c.id, c.name),
                  }));
                })(),
              },
              {
                key: 'plugins',
                icon: <ApiOutlined />,
                label: t('插件'),
                children: (() => {
                  const callable = installedPlugins.filter((p) => p.callable !== false);
                  if (callable.length === 0) {
                    return [{ key: 'plugins-empty', label: t('暂无已安装插件'), disabled: true }];
                  }
                  return callable.map((p) => ({
                    key: `plugin-${p.install_id}`,
                    icon: <PluginAvatar icon={p.icon} size={20} round />,
                    label: p.name,
                    onClick: () => onPickPluginFromMenu(p),
                  }));
                })(),
              },
              ...(activeLocalMode
                ? []
                : [
                    { type: 'divider' as const },
                    {
                      key: 'myspace',
                      icon: <CloudDownloadOutlined />,
                      label: t('从我的空间导入'),
                      onClick: () => setMySpaceImportOpen(true),
                    },
                  ]),
            ];
            return (
              <>
                <Dropdown
                  trigger={['click']}
                  placement="topRight"
                  overlayClassName="jx-attachMenu"
                  onOpenChange={(open) => {
                    setAttachOpen(open);
                    if (!open) return;
                    if (!disableMention && agents.length === 0) void fetchAgents();
                  }}
                  menu={{ items }}
                >
                  <button
                    type="button"
                    className={`jx-attachBtn${attachOpen ? ' open' : ''}`}
                    title={t('添加文件')}
                    aria-label={t('添加文件')}
                  >
                    <IconPlus size={16} className="jx-attachIcon" />
                  </button>
                </Dropdown>
                <MySpaceImportModal draftKey={state.draftKey} open={mySpaceImportOpen} onClose={() => setMySpaceImportOpen(false)} />
                {/* Toolbar "create personal project" in-place modal: after a successful
                    creation, automatically binds the current chat to the new project
                    (not rendered on the project page — the project selector dropdown is
                    hidden there and the chat is fixed to the current project) */}
                {!projectComposer && (
                  <CreateProjectModal
                    onCreated={(pid) => {
                      const created = useProjectStore.getState().list.find((p) => p.project_id === pid);
                      bindChatProject(currentChatId, pid, created?.name || t('项目'));
                    }}
                  />
                )}
              </>
            );
          })()}


          {/* 提示词中心：社区版没有这个能力；商业版按 Config「权限配置 → 应用可见范围」
              的 prompt_hub 位放行（allowed_apps 为空 = 不限制，等同全员可见）。 */}
          {!isCE && isAppAllowed('prompt_hub') && (
            <button
              type="button"
              className={`jx-composerChip jx-promptHubBtn${promptHubOpen ? ' active' : ''}`}
              onClick={() => setPromptHubOpen(!promptHubOpen)}
              aria-label={t('提示词中心')}
              aria-pressed={promptHubOpen}
            >
              <img src="/home/prompt.svg" alt="" className="jx-promptHubIcon" />
              <span className="jx-composerChip-label">{t('提示词中心')}</span>
            </button>
          )}

          {!projectComposer && (() => {
            // Project selector dropdown: default (no project bound) / bound to a project / create a new personal project.
            // Binding state uses chat.projectId as the single source of truth; project_id is attached automatically when sending messages.
            const projectMenuItems = [
              {
                key: 'proj-group',
                type: 'group' as const,
                label: t('项目'),
                children: [
                  {
                    key: 'proj-default',
                    label: (
                      <div className="jx-projectOption">
                        <FolderOutlined className="jx-projectOptionIcon" />
                        <span className="jx-projectOptionName">{t('默认')}</span>
                        {!boundProjectId && <img src="/home/check.svg" alt="" className="jx-modeCheckIcon" />}
                      </div>
                    ),
                    onClick: () => unbindChatProject(currentChatId),
                  },
                  ...projects.map((p) => ({
                    key: `proj-${p.project_id}`,
                    label: (
                      <div className="jx-projectOption">
                        {(p.kind as string) === 'local'
                          ? <LaptopOutlined className="jx-projectOptionIcon" />
                          : <FolderOutlined className="jx-projectOptionIcon" />}
                        <span className="jx-projectOptionName" title={p.name}>{p.name}</span>
                        {boundProjectId === p.project_id && <img src="/home/check.svg" alt="" className="jx-modeCheckIcon" />}
                      </div>
                    ),
                    onClick: () => onPickProject(p.project_id, p.name),
                  })),
                ],
              },
              { type: 'divider' as const },
              // 新建入口严格跟随安装时选择的运行形态：纯本机只显示本地项目，纯云端
              // 只显示云端项目，只有双模式同时显示两者。网页端保持原来的个人项目入口。
              ...(isDesktopShell
                ? [
                    ...(canCreateCloudProject
                      ? [{
                          key: 'proj-new-cloud',
                          label: (
                            <div className="jx-projectOption">
                              <FolderAddOutlined className="jx-projectOptionIcon" />
                              <span className="jx-projectOptionName">{t('新建云端项目')}</span>
                            </div>
                          ),
                          onClick: () => setProjectCreateModalOpen(true),
                        }]
                      : []),
                    ...(canCreateLocalProject
                      ? [{
                          key: 'proj-new-local',
                          label: (
                            <div className="jx-projectOption">
                              <LaptopOutlined className="jx-projectOptionIcon" />
                              <span className="jx-projectOptionName">{t('新建本地项目')}</span>
                            </div>
                          ),
                          onClick: () => {
                            window.location.href = '/__desktop/pick-local-folder';
                          },
                        }]
                      : []),
                  ]
                : [
                    {
                      key: 'proj-new',
                      label: (
                        <div className="jx-projectOption">
                          <FolderAddOutlined className="jx-projectOptionIcon" />
                          <span className="jx-projectOptionName">{t('新建个人项目')}</span>
                        </div>
                      ),
                      onClick: () => setProjectCreateModalOpen(true),
                    },
                  ]),
            ];
            return (
              <Dropdown
                trigger={['click']}
                placement="topLeft"
                overlayClassName="jx-projectMenu"
                onOpenChange={(open) => {
                  setProjectOpen(open);
                  if (open && projects.length === 0) void fetchProjects();
                }}
                menu={{ items: projectMenuItems }}
              >
                <button
                  type="button"
                  className={`jx-composerChip jx-projectDropBtn${boundProjectId ? ' bound' : ''}${projectOpen ? ' open' : ''}`}
                  aria-label={boundProjectId
                    ? t('本对话属于项目「{name}」，点击切换', { name: boundProjectName })
                    : t('选择项目，当前为默认（不归属项目）')}
                  title={t('选择项目')}
                >
                  {boundProjectId
                    ? <FolderOpenOutlined className="jx-projectDropIcon" />
                    : <FolderOutlined className="jx-projectDropIcon" />}
                  <span className="jx-projectDropName jx-composerChip-label">{boundProjectId ? boundProjectName : t('默认')}</span>
                  <ChipChevron />
                </button>
              </Dropdown>
            );
          })()}

      {!projectComposer && <ApprovalPill />}

          {/* Mode chips report "you are in this mode" and carry their own ✕ at the top-right —
              that ✕ is the way out, so an accidentally started mode is always one click from
              being cancelled. The chip only renders while its mode is actually running; a plan
              chat keeping its historical plan cards shows nothing once back to normal chat. */}
          {!projectComposer && planMode && (
            <ComposerModeChip
              icon={<OrderedListOutlined className="jx-planModeIcon" />}
              label={t('计划模式')}
              title={t('计划模式：AI 将自动分解任务为多步骤并逐步执行')}
              closeLabel={t('关闭计划模式：切换为普通对话')}
              onClose={() => onCloseMode('plan')}
            />
          )}

          {!projectComposer && batchModeOn && (
            <ComposerModeChip
              icon={<ThunderboltOutlined className="jx-planModeIcon" />}
              label={t('批量执行')}
              title={t('批量执行模式：描述要批量处理的对象与任务，AI 会自动生成可确认的执行计划')}
              closeLabel={t('关闭批量执行：切换为普通对话')}
              onClose={() => onCloseMode('batch')}
            />
          )}

          {!projectComposer && workflowModeOn && (
            <ComposerModeChip
              icon={<PartitionOutlined className="jx-planModeIcon" />}
              label={t('工作流模式')}
              title={t('工作流模式：面对成百上千个同类工作项时，AI 会写一段作业脚本交给后台并发处理，进度记在台账上，中断可续跑')}
              closeLabel={t('关闭工作流模式：切换为普通对话')}
              onClose={() => onCloseMode('workflow')}
            />
          )}

          {showLoopEntry && loopMode && (
            <ComposerModeChip
              icon={<SyncOutlined className="jx-planModeIcon" />}
              label={t('自主循环')}
              title={t('自主循环：描述一个可验证目标，AI 会反复迭代、自我修正，达标或触预算即停')}
              closeLabel={t('关闭自主循环：切换为普通对话')}
              onClose={() => setLoopMode(false)}
            />
          )}

          {projectComposer && activeMode && (
            <ComposerModeChip
              icon={activeMode === 'plan'
                ? <OrderedListOutlined className="jx-planModeIcon" />
                : <ThunderboltOutlined className="jx-planModeIcon" />}
              label={activeMode === 'plan' ? t('计划模式') : t('批量执行')}
              title={t('发送后将以该模式在本项目内开始对话')}
              closeLabel={t('取消该模式')}
              onClose={() => onCloseMode(activeMode)}
            />
          )}

          <div className="jx-composerSpacer" style={{ flex: 1 }} />

          {/* 模型 + 思考强度：两级菜单，收起态一个 chip 同时报出两者 */}
          <ModelEffortChip />

          {/* Context-usage ring: estimated context-window occupancy for the current conversation */}
          {/* 上下文用量条是装饰性的：它要遍历整轮工具结果算 token，成本随内容涨。
              万一在这里抛错，它长在消息列表那圈 ContentErrorBoundary **之外**，
              会一路捅到顶层错误边界、整页变成"页面显示遇到异常"——为一条进度条
              赔上整个页面不值当。单独兜一层，出事就让它自己消失。 */}
          <ContentErrorBoundary fallback={null}>
            <ContextGauge draftKey={state.draftKey} />
          </ContentErrorBoundary>

          {/* While a run is active, an empty composer keeps the stop button; typing switches
              back to send so Enter/click can queue a follow-up without cancelling the run. */}
          <button
            className="jx-sendBtn"
            onClick={() => { if (showStopButton) { abort?.(); } else { sendFromComposer(); } }}
            /* 空输入 / 纯空格时按钮置灰：send() 本来就会 `if (!msg) return` 静默吞掉，
               但按钮看着可点，用户以为发出去了。附件不能单独成一条消息（send 的守卫
               同样要求正文非空），所以判据是正文 trim 后是否为空——命令 chip 例外，它自己
               就是一条完整消息（composerHasContent）。
               注意别动 showStopButton 分支：流式输出中空输入时这颗按钮是「中止」。 */
            disabled={!showStopButton && (forkPending || uploadingFiles.size > 0 || !composerHasContent)}
            aria-label={showStopButton ? t('中止') : t('发送')}
          >
            <AnimatePresence mode="wait" initial={false}>
              <motion.img
                key={showStopButton ? 'stop' : 'send'}
                src={showStopButton ? '/home/stop.svg' : '/home/send.svg'}
                alt=""
                className="jx-sendIcon"
                initial={{ scale: 0.6, opacity: 0, rotate: -90 }}
                animate={{ scale: 1, opacity: 1, rotate: 0 }}
                exit={{ scale: 0.6, opacity: 0, rotate: 90 }}
                transition={{ duration: DUR.fast, ease: 'easeOut' }}
              />
            </AnimatePresence>
          </button>
        </div>
  );
}
