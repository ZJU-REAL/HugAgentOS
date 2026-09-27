import { useState, type ReactNode } from 'react';
import { motion } from 'motion/react';
import { Button, Drawer, Switch, Tooltip } from 'antd';
import { DeleteOutlined, EditOutlined, LeftOutlined, RobotOutlined, UploadOutlined, ExportOutlined, KeyOutlined } from '@ant-design/icons';
import type { UserAgentItem, AvailableResources } from '../../stores/agentStore';
import { EditionAgentBadge, useEditionAgentPolicy } from '../../agentEdition';
import { ChannelBotsPanel } from '../settings/ChannelBotsPanel';
import { AgentApiModal } from './AgentApiModal';
import { AgentIcon } from './AgentIcon';
import { formatDateTime } from '../../utils/date';
import { mdToHtml } from '../../utils/markdown';
import { staggerStyle } from '../../utils/motionTokens';
import { DRILL_IN_DETAIL } from '../../utils/motionVariants';
import { t } from '../../i18n';

interface AgentDetailItem {
  label: string;
  value: string | string[];
  multiline?: boolean;
  markdown?: boolean;
  list?: boolean;
  emptyText?: string;
}

interface AgentDetailSection {
  key: string;
  title: string;
  items: AgentDetailItem[];
}

interface Props {
  selectedAgent: UserAgentItem;
  availableResources: AvailableResources | null;
  colorIndex: number;
  canEdit: boolean;
  canAddAgent: boolean;
  canUseApiKey: boolean;
  channelBotEnabled: boolean;
  navDir: 'detail' | 'list' | null;
  onBack: () => void;
  onEdit: () => void;
  startAgentChat: (agent: UserAgentItem) => void;
  handleExportAgent: (agent: UserAgentItem) => Promise<void>;
  handleToggleEnabled: (agent: UserAgentItem, enabled: boolean) => Promise<void>;
  handleDelete: (agent: UserAgentItem, event?: React.MouseEvent) => void;
  openSubmit: (agent: UserAgentItem) => void;
  submissionModal: ReactNode;
}
export function AgentDetailView({
  selectedAgent, availableResources, colorIndex, canEdit, canAddAgent, canUseApiKey,
  channelBotEnabled, navDir, onBack, onEdit, startAgentChat, handleExportAgent,
  handleToggleEnabled, handleDelete, openSubmit, submissionModal,
}: Props) {
  const [historyDrawerOpen, setHistoryDrawerOpen] = useState(false);
  const [botDrawerOpen, setBotDrawerOpen] = useState(false);
  const [apiOpen, setApiOpen] = useState(false);
  const editionAgentPolicy = useEditionAgentPolicy();
    const isBuiltin = selectedAgent.owner_type === 'builtin';
    const canToggle = canEdit || isBuiltin;
    const agentIdx = colorIndex;
    const skillNameMap = new Map((availableResources?.skills || []).map((item) => [item.id, item.name]));
    const mcpNameMap = new Map((availableResources?.mcp_servers || []).map((item) => [item.id, item.name]));
    const pluginNameMap = new Map((availableResources?.plugins || []).map((item) => [item.id, item.name]));
    const skillLabels = (selectedAgent.skill_ids || []).map((id) => skillNameMap.get(id) || id);
    const mcpLabels = (selectedAgent.mcp_server_ids || []).map((id) => mcpNameMap.get(id) || id);
    const pluginLabels = (selectedAgent.plugin_ids || []).map((id) => pluginNameMap.get(id) || id);
    const capabilityPolicy = typeof selectedAgent.extra_config?.capability_policy === 'string'
      ? selectedAgent.extra_config.capability_policy
      : '';
    const isReadOnlyBuiltin = isBuiltin && capabilityPolicy === 'read_only_intersection';
    const builtinCapabilityItems: AgentDetailItem[] = [
      {
        label: t('能力来源'),
        value: t('跟随主智能体（运行时动态加载）'),
      },
      {
        label: t('工具 (MCP)'),
        value: isReadOnlyBuiltin ? t('跟随主智能体，仅保留只读工具') : t('跟随主智能体'),
      },
      {
        label: t('技能'),
        value: isReadOnlyBuiltin ? t('不继承执行型技能') : t('跟随主智能体'),
      },
      {
        label: t('插件'),
        value: isReadOnlyBuiltin
          ? t('跟随主智能体，仅使用插件展开后的只读工具')
          : t('跟随主智能体，运行时展开为工具和技能'),
      },
      {
        label: t('知识库'),
        value: t('跟随主智能体，并受当前用户权限限制'),
      },
      {
        label: t('权限边界'),
        value: isReadOnlyBuiltin
          ? t('只读，不允许 Bash 或修改工作区')
          : t('不新增授权，受当前用户权限和会话开关限制'),
      },
    ];
    const version = selectedAgent.version || 'V1.0';
    const changeHistory = [...(selectedAgent.change_history || [])].reverse();
    const detailSections: AgentDetailSection[] = [
      {
        key: 'basic',
        title: t('基础信息'),
        items: [
          { label: t('名称'), value: selectedAgent.name || t('未填写') },
          { label: t('简介'), value: selectedAgent.description || t('未填写') },
          {
            label: t('创建者类型'),
            value: selectedAgent.owner_type === 'user'
              ? t('用户创建')
              : editionAgentPolicy.creatorLabel(selectedAgent),
          },
          { label: t('创建时间'), value: formatDateTime(selectedAgent.created_at, t('未记录')) },
        ],
      },
      {
        key: 'interaction',
        title: t('交互设定'),
        items: [
          { label: t('角色设定'), value: selectedAgent.system_prompt || t('未填写'), multiline: true, markdown: true },
          { label: t('开场白'), value: selectedAgent.welcome_message || t('未填写'), multiline: true },
        ],
      },
      {
        key: 'bindings',
        title: isBuiltin ? t('能力策略') : t('能力绑定'),
        items: isBuiltin
          ? builtinCapabilityItems
          : [
            { label: t('绑定工具 (MCP)'), value: mcpLabels, list: true, emptyText: t('未绑定工具') },
            { label: t('绑定技能'), value: skillLabels, list: true, emptyText: t('未绑定技能') },
            { label: t('绑定插件'), value: pluginLabels, list: true, emptyText: t('未绑定插件') },
          ],
      },
      {
        key: 'runtime',
        title: t('执行参数'),
        items: [
          { label: t('最大推理轮次'), value: String(selectedAgent.max_iters ?? 10) },
          { label: t('共享上下文'), value: (selectedAgent.extra_config || {}).shared_context ? t('已启用') : t('未启用') },
        ],
      },
    ];

    return (
      <motion.div
        key="detail"
        className="jx-agentPage jx-agentLibraryPage"
        {...(navDir === 'detail' ? DRILL_IN_DETAIL : { initial: false })}
      >
        <div className="jx-agentDetail-top">
          {/* Back button — outside the content area */}
          <button
            className="jx-agentDetail-backBtn"
            onClick={onBack}
          >
            <LeftOutlined style={{ fontSize: 14 }} />
          </button>

          <div className="jx-agentDetail-content">
            {/* Name row: icon + name + badge + [enable switch] */}
            <div className="jx-agentDetail-nameRow">
              <div className="jx-agentDetail-iconWrap">
                <AgentIcon agent={selectedAgent} size={44} colorIndex={agentIdx >= 0 ? agentIdx : 0} />
              </div>
              <span className="jx-agentDetail-name">{selectedAgent.name}</span>
              <EditionAgentBadge agent={selectedAgent} />
              <span className={`jx-agentDetail-badge${selectedAgent.is_enabled ? ' on' : ''}`}>
                {selectedAgent.is_enabled ? t('已启用') : t('未启用')}
              </span>
              {canToggle && (
                <div className="jx-agentDetail-enableRow">
                  <span className="jx-agentDetail-enableLabel">{t('启用')}</span>
                  <Switch
                    size="small"
                    checked={selectedAgent.is_enabled}
                    onChange={(v) => handleToggleEnabled(selectedAgent, v)}
                  />
                </div>
              )}
            </div>

            {!isBuiltin && (
              <div className="jx-agentDetail-versionRow">
                <div className="jx-agentDetail-versionLeft">
                  <div className="jx-agentDetail-version">{t('版本号：{ver}', { ver: version })}</div>
                  <Button
                    type="text"
                    size="small"
                    className="jx-agentDetail-versionAction"
                    onClick={() => setHistoryDrawerOpen(true)}
                  >
                    {t('变更记录')}
                  </Button>
                </div>
                <div className="jx-agentDetail-version jx-agentDetail-versionMeta">
                  {t('最近更新：{time}', { time: formatDateTime(selectedAgent.updated_at, t('未记录')) })}
                </div>
              </div>
            )}

            <hr className="jx-agentDetail-divider" />

            <div className="jx-agentDetail-sections">
              {detailSections.map((section) => (
                <section key={section.key} className="jx-agentDetail-section">
                  <div className="jx-agentDetail-sectionHead">
                    <h3 className="jx-agentDetail-sectionTitle">{section.title}</h3>
                  </div>
                  <div className="jx-agentDetail-grid">
                    {section.items.map((item) => (
                      <div
                        key={`${section.key}-${item.label}`}
                        className={`jx-agentDetail-field${item.multiline ? ' is-multiline' : ''}`}
                      >
                        <div className="jx-agentDetail-fieldLabel">{item.label}</div>
                        <div className="jx-agentDetail-fieldValue">
                          {item.list ? (
                            Array.isArray(item.value) && item.value.length > 0 ? (
                              <div className="jx-agentDetail-chipList">
                                {item.value.map((entry) => (
                                  <span key={`${item.label}-${entry}`} className="jx-agentDetail-chip">{entry}</span>
                                ))}
                              </div>
                            ) : (
                              <span className="jx-agentDetail-emptyText">{item.emptyText || t('未填写')}</span>
                            )
                          ) : item.markdown && typeof item.value === 'string' && item.value !== t('未填写') ? (
                            <div
                              className="jx-md jx-agentDetail-markdown"
                              dangerouslySetInnerHTML={{ __html: mdToHtml(item.value) }}
                            />
                          ) : (
                            <span className={item.value === t('未填写') || item.value === t('未记录') ? 'jx-agentDetail-emptyText' : ''}>
                              {item.value}
                            </span>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                </section>
              ))}

            </div>

            {/* Actions */}
            <div className="jx-agentDetail-actionsWrap">
              <div className="jx-agentDetail-actions">
                <Button
                  type="primary"
                  disabled={!selectedAgent.is_enabled}
                  onClick={() => startAgentChat(selectedAgent)}
                >
                  {t('开始对话')}
                </Button>
                <Tooltip title={t('导出')}>
                  <Button
                    aria-label={t('导出')}
                    icon={<ExportOutlined />}
                    className="jx-agentDetail-iconBtn"
                    onClick={() => void handleExportAgent(selectedAgent)}
                  />
                </Tooltip>
                {canEdit && canUseApiKey && (
                  <Tooltip title="API-Key">
                    <Button aria-label="API-Key" icon={<KeyOutlined />}
                      className="jx-agentDetail-iconBtn" onClick={() => setApiOpen(true)} />
                  </Tooltip>
                )}
                {canEdit && channelBotEnabled && (
                  <Tooltip title={t('绑定渠道机器人')}>
                    <Button
                      aria-label={t('绑定渠道机器人')}
                      icon={<RobotOutlined />}
                      className="jx-agentDetail-iconBtn"
                      onClick={() => setBotDrawerOpen(true)}
                    />
                  </Tooltip>
                )}
                {canEdit && (
                  <>
                    <Tooltip title={t('编辑')}>
                      <Button
                        aria-label={t('编辑')}
                        icon={<EditOutlined />}
                        className="jx-agentDetail-iconBtn"
                        onClick={() => onEdit()}
                      />
                    </Tooltip>
                    <Tooltip title={t('删除')}>
                      <Button
                        danger
                        aria-label={t('删除')}
                        icon={<DeleteOutlined />}
                        className="jx-agentDetail-iconBtn danger"
                        onClick={(e) => handleDelete(selectedAgent, e)}
                      />
                    </Tooltip>
                  </>
                )}
                {selectedAgent.owner_type === 'user' && canAddAgent && (
                  <Tooltip title={t('申请上架到智能体市场')}>
                    <Button
                      aria-label={t('申请上架')}
                      icon={<UploadOutlined />}
                      className="jx-agentDetail-iconBtn"
                      onClick={() => openSubmit(selectedAgent)}
                    />
                  </Tooltip>
                )}
              </div>
            </div>

            <AgentApiModal agent={selectedAgent} open={apiOpen} onClose={() => setApiOpen(false)} />
            {submissionModal}
            <Drawer
              title={t('渠道机器人')}
              placement="right"
              width={520}
              open={botDrawerOpen}
              onClose={() => setBotDrawerOpen(false)}
              destroyOnClose
            >
              <ChannelBotsPanel agentId={selectedAgent.agent_id} agentName={selectedAgent.name} />
            </Drawer>

            <Drawer
              title={t('变更记录')}
              placement="right"
              width={460}
              open={historyDrawerOpen}
              onClose={() => setHistoryDrawerOpen(false)}
              className="jx-agentHistoryDrawer"
            >
              {changeHistory.length > 0 ? (
                <div className="jx-agentDetail-historyList">
                  {changeHistory.map((item, index) => (
                    <div
                      key={`${item.timestamp}-${item.version || index}`}
                      className="jx-agentDetail-historyItem"
                      style={staggerStyle(index)}
                    >
                      <div className="jx-agentDetail-historyDot" aria-hidden="true" />
                      <div className="jx-agentDetail-historyBody">
                        <div className="jx-agentDetail-historyMeta">
                          {item.version ? (
                            <span className="jx-agentDetail-historyVersion">{item.version}</span>
                          ) : null}
                          <span className="jx-agentDetail-historyTime">{formatDateTime(item.timestamp, t('未记录'))}</span>
                        </div>
                        <div className="jx-agentDetail-historyInfoRow">
                          <span className="jx-agentDetail-historyInfoLabel">{t('操作人员')}</span>
                          <span className="jx-agentDetail-historyInfoValue">{item.operator_name || t('未知用户')}</span>
                        </div>
                        <div className="jx-agentDetail-historyInfoRow">
                          <span className="jx-agentDetail-historyInfoLabel">{t('操作时间')}</span>
                          <span className="jx-agentDetail-historyInfoValue">{formatDateTime(item.timestamp, t('未记录'))}</span>
                        </div>
                        <div className="jx-agentDetail-historyInfoRow">
                          <span className="jx-agentDetail-historyInfoLabel">{t('变更内容')}</span>
                          <span className="jx-agentDetail-historyInfoValue">{item.content}</span>
                        </div>
                        {item.details?.length > 0 ? (
                          <div className="jx-agentDetail-historyDetailList">
                            {item.details.map((detail, detailIndex) => (
                              <div
                                key={`${item.timestamp}-${detail.field}-${detailIndex}`}
                                className="jx-agentDetail-historyDetailItem"
                              >
                                <div className="jx-agentDetail-historyDetailField">{detail.field}</div>
                                <div className="jx-agentDetail-historyDetailValues">
                                  <div className="jx-agentDetail-historyDetailLine">
                                    <span className="jx-agentDetail-historyDetailTag">{t('修改前')}</span>
                                    <span className="jx-agentDetail-historyDetailText">{detail.before}</span>
                                  </div>
                                  <div className="jx-agentDetail-historyDetailLine">
                                    <span className="jx-agentDetail-historyDetailTag is-after">{t('修改后')}</span>
                                    <span className="jx-agentDetail-historyDetailText">{detail.after}</span>
                                  </div>
                                </div>
                              </div>
                            ))}
                          </div>
                        ) : null}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="jx-agentDetail-historyEmpty">{t('暂无变更记录')}</div>
              )}
            </Drawer>
          </div>
        </div>
      </motion.div>
    );
}
