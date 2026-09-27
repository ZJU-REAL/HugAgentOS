import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { motion } from 'motion/react';
import { Button, Input, Modal, Pagination, Tooltip, message, Select, Form, Tag, List, Empty, Dropdown } from 'antd';
import { PlusOutlined, SearchOutlined, DeleteOutlined, EditOutlined, RightOutlined, AppstoreAddOutlined, UploadOutlined, DownOutlined, ImportOutlined } from '@ant-design/icons';
import { useAgentStore, type UserAgentItem } from '../../stores/agentStore';
import { AgentMarketplaceModal } from './AgentMarketplaceModal';
import {
  getMarketplaceAgents, getMarketplaceAgentDetail, installMarketplaceAgent,
  submitAgentToMarketplace, getMyAgentSubmissions, withdrawAgentSubmission,
} from '../../api';
import { AGENT_MARKETPLACE_CATEGORIES } from '../../utils/constants';
import type { AgentMarketSubmission, AgentMarketplaceFetchers } from '../../types';

// User-side market transport: directly reuse api.ts's stable function references; a module-level constant suffices (no need for a per-render memo).
/** 每页卡片数——与技能页 / MCP 页 / 插件页保持一致。 */
const AGENTS_PAGE_SIZE = 12;

const USER_MARKET_FETCHERS: AgentMarketplaceFetchers = {
  loadList: getMarketplaceAgents,
  loadDetail: getMarketplaceAgentDetail,
  install: installMarketplaceAgent,
};
import { useCatalogStore } from '../../stores/catalogStore';
import { usePanel } from '../../routing/usePanel';
import { useChatStore } from '../../stores/chatStore';
import { useAuthStore } from '../../stores/authStore';
import { EditionAgentBadge, useEditionAgentPolicy } from '../../agentEdition';
import { nowId } from '../../storage';
import { staggerStyle } from '../../utils/motionTokens';
import { CardTail } from '../common/CardTail';
import { DRILL_IN_BACK } from '../../utils/motionVariants';
import { AgentCreatePage } from './AgentCreatePage';
import { usePanelHeader } from '../../hooks/usePageConfig';
import { ABILITY_TAB_TITLE } from '../catalog/abilityTabs';
import { t } from '../../i18n';
import { DeviceCapabilityBadge } from '../catalog/DeviceCapabilityBadge';
import { AgentIcon } from './AgentIcon';
import { AgentDetailView } from './AgentDetailView';
import { AgentListSkeleton, AgentDetailSkeleton } from './AgentPanelSkeletons';

const AGENT_DETAIL_ID_KEY = 'hugagent_agent_detail_id';

function loadDetailId() {
  return typeof window !== 'undefined' ? window.localStorage.getItem(AGENT_DETAIL_ID_KEY) : null;
}
function saveDetailId(id: string | null) {
  if (typeof window === 'undefined') return;
  if (id) window.localStorage.setItem(AGENT_DETAIL_ID_KEY, id);
  else window.localStorage.removeItem(AGENT_DETAIL_ID_KEY);
}

interface AgentPanelProps {
  /** 作为「能力中心」的一个 pane 内嵌渲染时为 true：不做 localStorage 详情恢复，进入能力中心一律回到列表。 */
  embedded?: boolean;
}

export function AgentPanel({ embedded = false }: AgentPanelProps = {}) {
  const {
    agents, loading, fetchAgents, deleteAgent, updateAgent, toggleBuiltinAgent, setCurrentAgent,
    fetchAvailableResources, availableResources, importAgents, exportAgent,
  } = useAgentStore();
  const panel = usePanel();
  const { panelEntryNonce, setPanel } = useCatalogStore();
  const { setCurrentChatId, updateStore } = useChatStore();
  const { authUser } = useAuthStore();
  const channelBotEnabled = authUser?.can_create_channel_bot === true;
  // Permission to self-create/install sub-agents (backend can_add_agent gates creation/market install/listing application).
  // When off, hide the "add sub-agent" entry; viewing and editing existing sub-agents is unaffected.
  const canAddAgent = authUser?.can_add_agent === true;
  const { title: agentsTitle, subtitle: agentsSubtitle } = usePanelHeader('agents', {
    title: ABILITY_TAB_TITLE.agents,
    subtitle: '选择与启用智能体，并查看其职责边界与路由提示',
  });

  const [search, setSearch] = useState('');
  const [agentsPage, setAgentsPage] = useState(1);
  // 内嵌进能力中心时不做 localStorage 详情恢复：那份记忆属于独立的智能体面板
  const persistDetail = !embedded;
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(
    persistDetail ? loadDetailId : null,
  );
  // undefined = list/detail, null = create page, UserAgentItem = edit page
  const [formPageAgent, setFormPageAgent] = useState<UserAgentItem | null | undefined>(undefined);
  // Distinguish "user clicks navigation" from "localStorage restore / panel reset": only the former plays the list↔detail transition
  const [navDir, setNavDir] = useState<'detail' | 'list' | null>(null);

  const editionAgentPolicy = useEditionAgentPolicy();

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
  }, [fetchAgents, fetchAvailableResources]);
  useEffect(() => {
    if (!persistDetail) return;
    saveDetailId(selectedAgentId);
  }, [persistDetail, selectedAgentId]);

  const canEditAgent = (a: UserAgentItem): boolean =>
    a.owner_type === 'user' || editionAgentPolicy.canManage(a);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    const list = agents.filter((a) => (
      a.is_enabled || a.owner_type === 'user' || a.owner_type === 'builtin'
      || editionAgentPolicy.includeInLibrary(a)
    ));
    if (!q) return list;
    return list.filter((a) => a.name.toLowerCase().includes(q) || (a.description || '').toLowerCase().includes(q));
  }, [agents, editionAgentPolicy, search]);

  // 分页：与技能页 / MCP 页同一套（每页 12 + antd Pagination），智能体多起来以后
  // 原来是一屏铺到底，翻不完也定位不到（问题 40）。
  const pagedAgents = useMemo(
    () => filtered.slice((agentsPage - 1) * AGENTS_PAGE_SIZE, agentsPage * AGENTS_PAGE_SIZE),
    [filtered, agentsPage],
  );

  // 搜索或数据变化把页码顶出范围时拉回第一页
  useEffect(() => {
    const maxPage = Math.max(1, Math.ceil(filtered.length / AGENTS_PAGE_SIZE));
    if (agentsPage > maxPage) setAgentsPage(1);
  }, [filtered.length, agentsPage]);

  const selectedAgent = useMemo(
    () => (selectedAgentId ? agents.find((a) => a.agent_id === selectedAgentId) ?? null : null),
    [selectedAgentId, agents],
  );

  useEffect(() => {
    if (selectedAgentId && !selectedAgent) setSelectedAgentId(null);
  }, [selectedAgentId, selectedAgent]);

  // 重新进入宿主面板时回到列表；内嵌时宿主是能力中心，独立挂载时宿主是智能体面板。
  const homePanel = embedded ? 'ability_center' : 'agents';
  useEffect(() => {
    if (panel !== homePanel) return;
    setSelectedAgentId(null);
    setFormPageAgent(undefined);
    setSearch('');
  }, [homePanel, panel, panelEntryNonce]);

  const importInputRef = useRef<HTMLInputElement>(null);

  async function handleImportFile(file: File) {
    try {
      const count = await importAgents(file);
      message.success(t('已导入 {n} 个智能体', { n: count }));
    } catch (err: unknown) {
      message.error(t('导入失败：{msg}', { msg: (err as Error).message }));
    }
  }

  async function handleExportAgent(agent: UserAgentItem) {
    try {
      const blob = await exportAgent(agent.agent_id);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${agent.name}.md`;
      a.click();
      URL.revokeObjectURL(url);
      message.success(t('智能体已导出：{name}.md', { name: agent.name }));
    } catch (err: unknown) {
      message.error(t('导出失败：{msg}', { msg: (err as Error).message }));
    }
  }

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
      title: t('删除智能体'), content: t('确定删除「{name}」吗？', { name: agent.name }),
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
      if (agent.owner_type === 'builtin') {
        await toggleBuiltinAgent(agent.agent_id, enabled);
      } else {
        await updateAgent(agent.agent_id, { is_enabled: enabled });
      }
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

  if (selectedAgent) {
    return <AgentDetailView key={selectedAgent.agent_id} selectedAgent={selectedAgent}
      availableResources={availableResources} colorIndex={agents.indexOf(selectedAgent)}
      canEdit={canEditAgent(selectedAgent)} canAddAgent={canAddAgent}
      canUseApiKey={authUser?.can_use_api_key === true} channelBotEnabled={channelBotEnabled}
      navDir={navDir} onBack={() => { setNavDir('list'); setSelectedAgentId(null); }}
      onEdit={() => setFormPageAgent(selectedAgent)} startAgentChat={startAgentChat}
      handleExportAgent={handleExportAgent} handleToggleEnabled={handleToggleEnabled}
      handleDelete={handleDelete} openSubmit={openSubmit}
      submissionModal={(
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
                  <Input.TextArea rows={2} maxLength={200} placeholder={t('一句话介绍这个智能体的用途')} />
                </Form.Item>
                <Form.Item name="note" label={t('给管理员的备注')}>
                  <Input.TextArea rows={2} maxLength={500} placeholder={t('可选')} />
                </Form.Item>
              </Form>
            </Modal>

      )} />;
  }

  // ── List view ────────────────────────────────────────────────
  return (
    <motion.div
      key="list"
      className="jx-agentPage jx-agentLibraryPage"
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
            <>
              <input
                ref={importInputRef}
                type="file"
                accept=".md,.zip,.json"
                hidden
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) void handleImportFile(file);
                  e.target.value = '';
                }}
              />
              <Dropdown
                menu={{
                  items: [
                    { key: 'create', icon: <PlusOutlined />, label: t('创建智能体'), onClick: () => setFormPageAgent(null) },
                    { key: 'market', icon: <AppstoreAddOutlined />, label: t('从智能体市场获取'), onClick: () => setMarketOpen(true) },
                    { key: 'import', icon: <ImportOutlined />, label: t('导入智能体'), onClick: () => importInputRef.current?.click() },
                    { key: 'mysubs', icon: <UploadOutlined />, label: t('我的上架申请'), onClick: openMySubs },
                  ],
                }}
              >
                <Button type="primary" icon={<PlusOutlined />} className="jx-agentPage-createBtn">
                  {t('添加智能体')} <DownOutlined />
                </Button>
              </Dropdown>
            </>
          )}
        </div>
      </div>

      {/* Card grid (container key=panelEntryNonce controls stagger replay; data updates like the enable toggle don't replay) */}
      {loading ? (
        <AgentListSkeleton />
      ) : filtered.length === 0 ? (
        <div className="jx-agentPage-empty jx-anim-fadeIn">{t('暂无智能体')}</div>
      ) : (
        <div
          className="jx-agentPage-grid jx-anim-stagger"
          style={{ '--stagger-step': '30ms' } as React.CSSProperties}
          key={`agents-${panelEntryNonce}`}
        >
          {pagedAgents.map((agent, idx) => {
            const canEdit = canEditAgent(agent);
            const canToggle = canEdit || agent.owner_type === 'builtin';
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
                      <EditionAgentBadge agent={agent} />
                      <DeviceCapabilityBadge kind="agent" runtimeName={agent.name} />
                    </div>
                    {/* 启停开关常驻，编辑/删除悬浮才显形；无权启停的智能体整个尾部都不出现 */}
                    {canToggle && (
                      <CardTail
                        checked={agent.is_enabled}
                        onChange={(v) => void handleToggleEnabled(agent, v)}
                        actions={canEdit && (
                          <>
                            <Tooltip title={t('编辑')}>
                              <button onClick={() => setFormPageAgent(agent)}><EditOutlined /></button>
                            </Tooltip>
                            <Tooltip title={t('删除')}>
                              <button className="danger" onClick={(e) => handleDelete(agent, e)}>
                                <DeleteOutlined />
                              </button>
                            </Tooltip>
                          </>
                        )}
                      />
                    )}
                    <RightOutlined className="jx-agentCard-disclosure" aria-hidden="true" />
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

      {filtered.length > AGENTS_PAGE_SIZE && (
        <div className="jx-sk-pagination">
          <Pagination
            current={agentsPage}
            pageSize={AGENTS_PAGE_SIZE}
            total={filtered.length}
            onChange={setAgentsPage}
            showSizeChanger={false}
            size="small"
          />
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
