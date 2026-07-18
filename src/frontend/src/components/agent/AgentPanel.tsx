import { useCallback, useEffect, useMemo, useState } from 'react';
import { motion } from 'motion/react';
import { Button, Drawer, Input, Modal, Skeleton, Switch, Tooltip, message, Select, Form, Tag, List, Empty, Dropdown } from 'antd';
import { PlusOutlined, SearchOutlined, DeleteOutlined, EditOutlined, LeftOutlined, RobotOutlined, AppstoreAddOutlined, UploadOutlined, DownOutlined } from '@ant-design/icons';
import { useAgentStore, type UserAgentItem } from '../../stores/agentStore';
import { AgentMarketplaceModal } from './AgentMarketplaceModal';
import {
  getMarketplaceAgents, getMarketplaceAgentDetail, installMarketplaceAgent,
  submitAgentToMarketplace, getMyAgentSubmissions, withdrawAgentSubmission,
} from '../../api';
import { AGENT_MARKETPLACE_CATEGORIES } from '../../utils/constants';
import type { AgentMarketSubmission, AgentMarketplaceFetchers } from '../../types';

// User-side market transport: directly reuse api.ts's stable function references; a module-level constant suffices (no need for a per-render memo).
const USER_MARKET_FETCHERS: AgentMarketplaceFetchers = {
  loadList: getMarketplaceAgents,
  loadDetail: getMarketplaceAgentDetail,
  install: installMarketplaceAgent,
};
import { useCatalogStore } from '../../stores/catalogStore';
import { useChatStore } from '../../stores/chatStore';
import { useAuthStore } from '../../stores/authStore';
import { ChannelBotsPanel } from '../settings/ChannelBotsPanel';
import { listMyTeamsForProjects } from '../../api';
import { nowId } from '../../storage';
import { formatDateTime } from '../../utils/date';
import { mdToHtml } from '../../utils/markdown';
import { staggerStyle } from '../../utils/motionTokens';
import { DRILL_IN_BACK, DRILL_IN_DETAIL } from '../../utils/motionVariants';
import { AgentCreatePage } from './AgentCreatePage';
import { usePanelHeader } from '../../hooks/usePageConfig';
import { t } from '../../i18n';

const AGENT_ICON_MAP: Record<string, string> = {
  '报告生成子智能体': '/home/agent-icons/report.svg',
  '知识检索子智能体': '/home/agent-icons/knowledge.svg',
  '报告撰写': '/home/agent-icons/report-writing.svg',
  '知识检索': '/home/agent-icons/knowledge-search.svg',
  '智能问答': '/home/agent-icons/qa.svg',
  '数据分析': '/home/agent-icons/data-analysis.svg',
  '政策解读': '/home/agent-icons/policy.svg',
  '信息提取': '/home/agent-icons/info-extract.svg',
  '企业画像': '/home/agent-icons/company-profile.svg',
  '产业链分析': '/home/agent-icons/industry-chain.svg',
  '材料分析': '/home/agent-icons/material-analysis.svg',
  '流程指引': '/home/agent-icons/process-guide.svg',
};

const RANDOM_ICONS = [
  'Frame 442.svg', 'Frame 443.svg', 'Frame 444.svg', 'Frame 445.svg',
  'Frame 446.svg', 'Frame 447.svg', 'Frame 448.svg', 'Frame 449.svg',
  'Frame 450.svg', 'Frame 451.svg', 'Frame 452.svg', 'Frame 453.svg',
  'Frame 454.svg', 'Frame 455.svg', 'Frame 456.svg', 'Frame 457.svg',
  'Frame 458.svg', 'Frame 459.svg', 'Frame 460.svg', 'Frame 461.svg',
  'Frame 462.svg', 'Frame 463.svg', 'Frame 464.svg', 'Frame 465.svg',
  'Frame 466.svg', 'Frame 467.svg', 'Frame 468.svg', 'Frame 469.svg',
  'Frame 470.svg', 'Frame 471.svg', 'Frame 472.svg',
];

/** Deterministic hash from string → index into RANDOM_ICONS */
function hashToIconIndex(str: string): number {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    hash = ((hash << 5) - hash + str.charCodeAt(i)) | 0;
  }
  return Math.abs(hash) % RANDOM_ICONS.length;
}

/** Get a random icon URL based on agent id or name */
export function getRandomIconUrl(key: string): string {
  const fileName = RANDOM_ICONS[hashToIconIndex(key)];
  return `/home/random-icons/${encodeURIComponent(fileName)}`;
}

const AGENT_DETAIL_ID_KEY = 'hugagent_agent_detail_id';

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

function loadDetailId() {
  return typeof window !== 'undefined' ? window.localStorage.getItem(AGENT_DETAIL_ID_KEY) : null;
}
function saveDetailId(id: string | null) {
  if (typeof window === 'undefined') return;
  id ? window.localStorage.setItem(AGENT_DETAIL_ID_KEY, id) : window.localStorage.removeItem(AGENT_DETAIL_ID_KEY);
}

// The avatar may be an image address (/path, http(s), data:, blob:) or an emoji/text (market sub-agents
// use a Cherry emoji as their avatar). Only image addresses go through <img>; otherwise render as text, to avoid an emoji being
// treated as an image URL and causing a broken image.
function isImageAvatar(src: string): boolean {
  return /^(https?:|data:|blob:|\/)/.test(src.trim());
}

function AgentIcon({ agent, size }: { agent: UserAgentItem; size: number; colorIndex?: number }) {
  const radius = size < 36 ? '50%' : 8;
  const avatar = agent.avatar?.trim();
  if (avatar) {
    if (isImageAvatar(avatar)) {
      return <img src={avatar} alt="" width={size} height={size}
        style={{ borderRadius: radius, objectFit: 'cover', display: 'block' }} />;
    }
    // emoji / text avatar
    return (
      <span style={{
        width: size, height: size, borderRadius: radius,
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        fontSize: Math.round(size * 0.62), lineHeight: 1,
        background: 'var(--color-primary-light, #EBF2FF)',
      }}>{avatar}</span>
    );
  }
  const mapped = AGENT_ICON_MAP[agent.name];
  if (mapped) {
    return <img src={mapped} alt="" width={size} height={size}
      style={{ borderRadius: radius, objectFit: 'cover', display: 'block' }} />;
  }
  // Fallback: deterministic random icon based on agent_id or name
  const iconUrl = getRandomIconUrl(agent.agent_id || agent.name);
  return <img src={iconUrl} alt="" width={size} height={size}
    style={{ borderRadius: radius, objectFit: 'cover', display: 'block' }} />;
}

function AgentListSkeleton() {
  return (
    <div className="jx-agentPage-grid">
      {Array.from({ length: 6 }, (_, idx) => (
        <div key={idx} className="jx-agentCard jx-agentCardSkeleton" aria-hidden="true">
          <div className="jx-agentCard-body">
            <div className="jx-agentCard-head">
              <Skeleton.Avatar active size={28} shape="circle" />
              <div className="jx-agentCardSkeletonMeta">
                <Skeleton.Input active size="small" className="jx-agentCardSkeletonTitle" />
                <Skeleton.Input active size="small" className="jx-agentCardSkeletonBadge" />
              </div>
            </div>
            <Skeleton active paragraph={{ rows: 2, width: ['92%', '74%'] }} title={false} />
          </div>
        </div>
      ))}
    </div>
  );
}

function AgentDetailSkeleton() {
  return (
    <div className="jx-agentPage">
      <div className="jx-agentDetail-top">
        <button className="jx-agentDetail-backBtn" type="button" aria-hidden="true">
          <LeftOutlined style={{ fontSize: 14 }} />
        </button>
        <div className="jx-agentDetail-content">
          <div className="jx-agentDetail-nameRow">
            <Skeleton.Avatar active size={44} shape="square" />
            <Skeleton.Input active className="jx-agentDetailSkeletonTitle" />
            <Skeleton.Input active size="small" className="jx-agentDetailSkeletonBadge" />
          </div>
          <Skeleton.Input active size="small" className="jx-agentDetailSkeletonVersion" />
          <hr className="jx-agentDetail-divider" />
          <div className="jx-agentDetail-sections">
            {Array.from({ length: 3 }, (_, idx) => (
              <section key={idx} className="jx-agentDetail-section" aria-hidden="true">
                <div className="jx-agentDetail-sectionHead">
                  <Skeleton.Input active size="small" className="jx-agentDetailSkeletonSectionTitle" />
                </div>
                <div className="jx-agentDetail-grid">
                  <div className="jx-agentDetail-field">
                    <Skeleton active paragraph={{ rows: 2, width: ['30%', '80%'] }} title={false} />
                  </div>
                  <div className="jx-agentDetail-field">
                    <Skeleton active paragraph={{ rows: 2, width: ['34%', '68%'] }} title={false} />
                  </div>
                </div>
              </section>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

export function AgentPanel() {
  const {
    agents, loading, fetchAgents, deleteAgent, updateAgent, setCurrentAgent,
    fetchAvailableResources, availableResources,
  } = useAgentStore();
  const { panel, panelEntryNonce, setPanel } = useCatalogStore();
  const { setCurrentChatId, updateStore } = useChatStore();
  const { authUser } = useAuthStore();
  const channelBotEnabled = authUser?.can_create_channel_bot === true;
  // Permission to self-create/install sub-agents (backend can_add_agent gates creation/market install/listing application).
  // When off, hide the "add sub-agent" entry; viewing and editing existing sub-agents is unaffected.
  const canAddAgent = authUser?.can_add_agent === true;
  const { title: agentsTitle, subtitle: agentsSubtitle } = usePanelHeader('agents', {
    title: '子智能体',
    subtitle: '选择与启用子智能体，并查看其职责边界与路由提示',
  });

  const [search, setSearch] = useState('');
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(loadDetailId);
  const [historyDrawerOpen, setHistoryDrawerOpen] = useState(false);
  const [botDrawerOpen, setBotDrawerOpen] = useState(false);
  // undefined = list/detail, null = create page, UserAgentItem = edit page
  const [formPageAgent, setFormPageAgent] = useState<UserAgentItem | null | undefined>(undefined);
  // Distinguish "user clicks navigation" from "localStorage restore / panel reset": only the former plays the list↔detail transition
  const [navDir, setNavDir] = useState<'detail' | 'list' | null>(null);

  // Teams where I am owner/admin — determines whether team sub-agents can be edited/deleted
  const [managerTeamIds, setManagerTeamIds] = useState<Set<string>>(new Set());

  // Sub-agent market / listing application
  const [marketOpen, setMarketOpen] = useState(false);
  const [mySubsOpen, setMySubsOpen] = useState(false);
  const [mySubs, setMySubs] = useState<AgentMarketSubmission[]>([]);
  const [mySubsLoading, setMySubsLoading] = useState(false);
  const [submitAgent, setSubmitAgent] = useState<UserAgentItem | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitForm] = Form.useForm();

  const reloadMySubs = useCallback(async () => {
    setMySubsLoading(true);
    try { setMySubs(await getMyAgentSubmissions()); }
    catch (e) { message.error((e as Error).message || t('加载上架申请失败')); }
    finally { setMySubsLoading(false); }
  }, []);

  const openMySubs = useCallback(() => { setMySubsOpen(true); void reloadMySubs(); }, [reloadMySubs]);

  const openSubmit = useCallback((agent: UserAgentItem) => {
    submitForm.resetFields();
    submitForm.setFieldsValue({ category: AGENT_MARKETPLACE_CATEGORIES[0], summary: agent.description || '' });
    setSubmitAgent(agent);
  }, [submitForm]);

  const handleSubmitToMarket = useCallback(async () => {
    if (!submitAgent) return;
    const values = await submitForm.validateFields();
    setSubmitting(true);
    try {
      await submitAgentToMarketplace({ agent_id: submitAgent.agent_id, category: values.category, summary: values.summary, note: values.note });
      message.success(t('已提交上架申请，等待管理员审核'));
      setSubmitAgent(null);
    } catch (e) { message.error((e as Error).message || t('提交失败')); }
    finally { setSubmitting(false); }
  }, [submitAgent, submitForm]);

  const handleWithdrawSub = useCallback(async (id: string) => {
    try { await withdrawAgentSubmission(id); message.success(t('已撤回')); await reloadMySubs(); }
    catch (e) { message.error((e as Error).message || t('撤回失败')); }
  }, [reloadMySubs]);

  useEffect(() => {
    void fetchAgents();
    void fetchAvailableResources();
    listMyTeamsForProjects()
      .then((teams) => setManagerTeamIds(new Set(teams.map((tm) => tm.team_id))))
      .catch(() => { /* ignore */ });
  }, []);
  useEffect(() => { saveDetailId(selectedAgentId); }, [selectedAgentId]);

  // Team sub-agents: manageable only when the current user is owner/admin of that team
  const canEditAgent = (a: UserAgentItem): boolean =>
    a.owner_type === 'user' || (a.owner_type === 'team' && !!a.team_id && managerTeamIds.has(a.team_id));

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    // team agents are delivered by the backend per member/manager scope (members only get enabled ones); include all of them here
    const list = agents.filter((a) => a.is_enabled || a.owner_type === 'user' || a.owner_type === 'team');
    if (!q) return list;
    return list.filter((a) => a.name.toLowerCase().includes(q) || (a.description || '').toLowerCase().includes(q));
  }, [agents, search]);

  const selectedAgent = useMemo(
    () => (selectedAgentId ? agents.find((a) => a.agent_id === selectedAgentId) ?? null : null),
    [selectedAgentId, agents],
  );

  useEffect(() => {
    if (selectedAgentId && !selectedAgent) setSelectedAgentId(null);
  }, [selectedAgentId, selectedAgent]);
  useEffect(() => {
    setHistoryDrawerOpen(false);
    setBotDrawerOpen(false);
  }, [selectedAgentId]);

  useEffect(() => {
    if (panel !== 'agents') return;
    setSelectedAgentId(null);
    setHistoryDrawerOpen(false);
    setFormPageAgent(undefined);
    setSearch('');
  }, [panel, panelEntryNonce]);

  function startAgentChat(agent: UserAgentItem) {
    setCurrentAgent(agent);
    const chatId = nowId('agent');
    updateStore((prev) => ({
      chats: {
        ...prev.chats,
        [chatId]: {
          id: chatId, title: agent.name,
          createdAt: Date.now(), updatedAt: Date.now(),
          messages: [], agentId: agent.agent_id, agentName: agent.name,
        },
      },
      order: [chatId, ...(prev.order || [])],
    }));
    setCurrentChatId(chatId);
    setPanel('chat');
  }

  function handleDelete(agent: UserAgentItem, e?: React.MouseEvent) {
    e?.stopPropagation();
    Modal.confirm({
      title: t('删除子智能体'), content: t('确定删除「{name}」吗？', { name: agent.name }),
      okText: t('删除'), okButtonProps: { danger: true }, cancelText: t('取消'),
      onOk: async () => {
        try {
          await deleteAgent(agent.agent_id);
          message.success(t('已删除'));
          if (selectedAgentId === agent.agent_id) {
            setNavDir('list');
            setSelectedAgentId(null);
          }
        } catch (err: unknown) {
          message.error((err as Error).message || t('删除失败'));
        }
      },
    });
  }

  async function handleToggleEnabled(agent: UserAgentItem, enabled: boolean) {
    try {
      await updateAgent(agent.agent_id, { is_enabled: enabled });
    } catch (err: unknown) {
      message.error((err as Error).message || t('操作失败'));
    }
  }

  // ── Create / Edit form page ──────────────────────────────────
  if (formPageAgent !== undefined) {
    return (
      <AgentCreatePage
        agent={formPageAgent}
        onBack={() => setFormPageAgent(undefined)}
        onCreated={() => setFormPageAgent(undefined)}
      />
    );
  }

  if (loading && selectedAgentId) {
    return <AgentDetailSkeleton />;
  }

  // ── Detail view ──────────────────────────────────────────────
  if (selectedAgent) {
    const canEdit = canEditAgent(selectedAgent);
    const agentIdx = agents.findIndex((a) => a.agent_id === selectedAgent.agent_id);
    const skillNameMap = new Map((availableResources?.skills || []).map((item) => [item.id, item.name]));
    const mcpNameMap = new Map((availableResources?.mcp_servers || []).map((item) => [item.id, item.name]));
    const pluginNameMap = new Map((availableResources?.plugins || []).map((item) => [item.id, item.name]));
    const skillLabels = (selectedAgent.skill_ids || []).map((id) => skillNameMap.get(id) || id);
    const mcpLabels = (selectedAgent.mcp_server_ids || []).map((id) => mcpNameMap.get(id) || id);
    const pluginLabels = (selectedAgent.plugin_ids || []).map((id) => pluginNameMap.get(id) || id);
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
              : selectedAgent.owner_type === 'team'
                ? t('团队共享')
                : t('系统内置'),
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
        title: t('能力绑定'),
        items: [
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
        className="jx-agentPage"
        {...(navDir === 'detail' ? DRILL_IN_DETAIL : { initial: false })}
      >
        <div className="jx-agentDetail-top">
          {/* Back button — outside the content area */}
          <button
            className="jx-agentDetail-backBtn"
            onClick={() => { setNavDir('list'); setSelectedAgentId(null); }}
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
              <span className={`jx-agentDetail-badge${selectedAgent.is_enabled ? ' on' : ''}`}>
                {selectedAgent.is_enabled ? t('已启用') : t('未启用')}
              </span>
              {canEdit && (
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
                <Button type="primary" onClick={() => startAgentChat(selectedAgent)}>{t('开始对话')}</Button>
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
                        onClick={() => setFormPageAgent(selectedAgent)}
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
                  <Tooltip title={t('申请上架到子智能体市场')}>
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

            <Modal
              title={t('申请上架「{name}」', { name: selectedAgent.name })}
              open={!!submitAgent}
              onCancel={() => setSubmitAgent(null)}
              onOk={() => void handleSubmitToMarket()}
              okText={t('提交申请')}
              cancelText={t('取消')}
              confirmLoading={submitting}
              destroyOnHidden
            >
              <p style={{ color: 'var(--color-text-tertiary)', fontSize: 12, marginTop: 0 }}>
                {t('提交后由管理员审核，通过后将以社区共享形式上架，其他用户可安装。提交的是当前内容快照。')}
              </p>
              <Form form={submitForm} layout="vertical">
                <Form.Item name="category" label={t('上架分类')} rules={[{ required: true, message: t('请选择分类') }]}>
                  <Select options={AGENT_MARKETPLACE_CATEGORIES.map((c) => ({ label: c, value: c }))} />
                </Form.Item>
                <Form.Item name="summary" label={t('市场摘要')}>
                  <Input.TextArea rows={2} maxLength={200} placeholder={t('一句话介绍这个子智能体的用途')} />
                </Form.Item>
                <Form.Item name="note" label={t('给管理员的备注')}>
                  <Input.TextArea rows={2} maxLength={500} placeholder={t('可选')} />
                </Form.Item>
              </Form>
            </Modal>

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

  // ── List view ────────────────────────────────────────────────
  return (
    <motion.div
      key="list"
      className="jx-agentPage"
      {...(navDir === 'list' ? DRILL_IN_BACK : { initial: false })}
    >
      {/* Header */}
      <div className="jx-agentPage-header">
        <div>
          <h2 className="jx-agentPage-title">{agentsTitle}</h2>
          {agentsSubtitle ? <p className="jx-agentPage-subtitle">{agentsSubtitle}</p> : null}
        </div>
        <div className="jx-agentPage-headerRight">
          <Input
            placeholder={t('搜索')}
            prefix={<SearchOutlined style={{ color: '#B3BAC8' }} />}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            allowClear
            className="jx-agentPage-search"
          />
          {canAddAgent && (
            <Dropdown
              menu={{
                items: [
                  { key: 'create', icon: <PlusOutlined />, label: t('创建智能体'), onClick: () => setFormPageAgent(null) },
                  { key: 'market', icon: <AppstoreAddOutlined />, label: t('从智能体市场获取'), onClick: () => setMarketOpen(true) },
                  { key: 'mysubs', icon: <UploadOutlined />, label: t('我的上架申请'), onClick: openMySubs },
                ],
              }}
            >
              <Button type="primary" icon={<PlusOutlined />} className="jx-agentPage-createBtn">
                {t('添加子智能体')} <DownOutlined />
              </Button>
            </Dropdown>
          )}
        </div>
      </div>

      {/* Card grid (container key=panelEntryNonce controls stagger replay; data updates like the enable toggle don't replay) */}
      {loading ? (
        <AgentListSkeleton />
      ) : filtered.length === 0 ? (
        <div className="jx-agentPage-empty jx-anim-fadeIn">{t('暂无子智能体')}</div>
      ) : (
        <div
          className="jx-agentPage-grid jx-anim-stagger"
          style={{ '--stagger-step': '30ms' } as React.CSSProperties}
          key={`agents-${panelEntryNonce}`}
        >
          {filtered.map((agent, idx) => {
            const canEdit = canEditAgent(agent);
            return (
              <div key={agent.agent_id} className="jx-agentCard jx-card-lift"
                style={staggerStyle(idx)}
                onClick={() => { setNavDir('detail'); setSelectedAgentId(agent.agent_id); }}>
                <div className="jx-agentCard-body">
                  <div className="jx-agentCard-head">
                    {/* 28px circle icon */}
                    <div className="jx-agentCard-iconWrap">
                      <AgentIcon agent={agent} size={28} colorIndex={idx} />
                    </div>
                    {/* name + badge */}
                    <div className="jx-agentCard-nameRow">
                      <span className="jx-agentCard-name">{agent.name}</span>
                      {agent.owner_type === 'team' && (
                        <span className="jx-agentCard-badge">{t('团队')}</span>
                      )}
                      <span className={`jx-agentCard-badge${agent.is_enabled ? ' on' : ''}`}>
                        {agent.is_enabled ? t('已启用') : t('未启用')}
                      </span>
                    </div>
                    {/* edit / delete on hover */}
                    {canEdit && (
                      <span className="jx-agentCard-ops" onClick={(e) => e.stopPropagation()}>
                        <Tooltip title={t('编辑')}>
                          <button onClick={() => setFormPageAgent(agent)}><EditOutlined /></button>
                        </Tooltip>
                        <Tooltip title={t('删除')}>
                          <button className="danger" onClick={(e) => handleDelete(agent, e)}>
                            <DeleteOutlined />
                          </button>
                        </Tooltip>
                      </span>
                    )}
                  </div>
                  <p className="jx-agentCard-desc">
                    {agent.description || agent.system_prompt?.slice(0, 100) || t('暂无描述')}
                  </p>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <AgentMarketplaceModal
        open={marketOpen}
        onClose={() => setMarketOpen(false)}
        fetchers={USER_MARKET_FETCHERS}
        onInstalled={() => { void fetchAgents(); }}
      />

      <Modal
        title={t('我的上架申请')}
        open={mySubsOpen}
        onCancel={() => setMySubsOpen(false)}
        footer={null}
        width={640}
        destroyOnHidden
      >
        <List
          loading={mySubsLoading}
          dataSource={mySubs}
          locale={{ emptyText: <Empty description={t('暂无上架申请')} /> }}
          renderItem={(sub) => (
            <List.Item
              actions={sub.status !== 'approved'
                ? [<Button key="wd" type="link" danger size="small" onClick={() => void handleWithdrawSub(sub.submission_id)}>{t('撤回')}</Button>]
                : []}
            >
              <List.Item.Meta
                avatar={<span style={{ fontSize: 22 }}>{sub.avatar || '🤖'}</span>}
                title={
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                    {sub.name}
                    <Tag bordered={false}>{sub.category}</Tag>
                    {sub.status === 'pending' && <Tag color="processing">{t('审核中')}</Tag>}
                    {sub.status === 'approved' && <Tag color="success">{t('已上架')}</Tag>}
                    {sub.status === 'rejected' && <Tag color="error">{t('已驳回')}</Tag>}
                  </span>
                }
                description={sub.status === 'rejected' && sub.review_note
                  ? <span style={{ color: 'var(--color-error)' }}>{t('驳回理由：{r}', { r: sub.review_note })}</span>
                  : sub.summary || '—'}
              />
            </List.Item>
          )}
        />
      </Modal>
    </motion.div>
  );
}
