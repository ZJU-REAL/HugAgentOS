import { useCallback, useEffect, useMemo, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { Modal, Input, Tag, Button, Empty, Spin, Typography, Pagination, Tooltip, Popconfirm, Switch, message } from 'antd';
import { SearchOutlined, FireOutlined, CheckOutlined, LeftOutlined, DeleteOutlined, ApiOutlined, ToolOutlined, AppstoreOutlined, EyeOutlined } from '@ant-design/icons';
import { t } from '../../i18n';
import type { MarketplaceAgent, MarketplaceAgentDetail, AgentMarketplaceFetchers, MarketVisibilityValue } from '../../types';
import { mdToHtml } from '../../utils/markdown';
import { SPRING, staggerStyle } from '../../utils/motionTokens';
import { getOntologyBuildFailure, type OntologyBuildFailure } from '../../utils/apiError';
import { OntologyBuildValidationModal, VisibilityScopeModal } from '../common';

// Sub-agent marketplace modal: browse preset/community sub-agents, view their prompts and capability bindings, and install (clone) them.
// The transport (user / admin) is injected via fetchers, reusing the skill marketplace's jx-mk-* styles.
const MARKET_PAGE_SIZE = 12;

function AgentMarketIcon({ avatar, size }: { avatar?: string; size?: number }) {
  const s = size || 40;
  return (
    <div
      className="jx-mk-emojiAvatar"
      style={{
        width: s, height: s, borderRadius: 12, display: 'flex', alignItems: 'center',
        justifyContent: 'center', fontSize: Math.round(s * 0.55), flexShrink: 0,
        background: 'var(--color-primary-light, #EBF2FF)',
      }}
    >
      {avatar || '🤖'}
    </div>
  );
}

function CapabilityTags({ item }: { item: MarketplaceAgent }) {
  return (
    <span className="jx-mk-capTags" style={{ display: 'inline-flex', gap: 8, color: 'var(--color-text-tertiary)', fontSize: 12 }}>
      {item.skill_count > 0 && <span><ToolOutlined /> {t('技能 {n}', { n: item.skill_count })}</span>}
      {item.mcp_count > 0 && <span><ApiOutlined /> {t('工具 {n}', { n: item.mcp_count })}</span>}
      {item.plugin_count > 0 && <span><AppstoreOutlined /> {t('插件 {n}', { n: item.plugin_count })}</span>}
    </span>
  );
}

export function AgentMarketplaceModal({
  open,
  onClose,
  fetchers,
  onInstalled,
  scopeLabel,
}: {
  open: boolean;
  onClose: () => void;
  fetchers: AgentMarketplaceFetchers;
  onInstalled?: () => void;
  scopeLabel?: string;
}) {
  const [loading, setLoading] = useState(false);
  const [items, setItems] = useState<MarketplaceAgent[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [query, setQuery] = useState('');
  const [category, setCategory] = useState<string>('全部');
  const [page, setPage] = useState(1);
  const [installingSlug, setInstallingSlug] = useState<string | null>(null);
  const [detail, setDetail] = useState<MarketplaceAgentDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [buildFailure, setBuildFailure] = useState<OntologyBuildFailure | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchers.loadList();
      setItems(res.items || []);
      setCategories(res.categories || []);
    } catch (e) {
      message.error((e as Error).message || t('加载子智能体市场失败'));
    } finally {
      setLoading(false);
    }
  }, [fetchers]);

  useEffect(() => {
    if (open) {
      setQuery('');
      setCategory('全部');
      setPage(1);
      setDetail(null);
      void load();
    }
  }, [open, load]);

  useEffect(() => { setPage(1); }, [query, category]);

  const openDetail = useCallback(async (slug: string) => {
    setDetailLoading(true);
    setDetail(null);
    try {
      setDetail(await fetchers.loadDetail(slug));
    } catch (e) {
      message.error((e as Error).message || t('加载详情失败'));
    } finally {
      setDetailLoading(false);
    }
  }, [fetchers]);

  const markInstalled = useCallback((slug: string) => {
    setItems((prev) => prev.map((it) => (it.slug === slug ? { ...it, installed: true } : it)));
    setDetail((prev) => (prev && prev.slug === slug ? { ...prev, installed: true } : prev));
  }, []);

  const doInstall = useCallback(
    async (agent: MarketplaceAgent) => {
      setInstallingSlug(agent.slug);
      try {
        const r = await fetchers.install(agent.slug);
        const report = r.install_report;
        const dropped = report?.dropped?.length || 0;
        const needsSecret = report?.needs_secret?.length || 0;
        if (dropped || needsSecret) {
          const parts: string[] = [];
          if (needsSecret) parts.push(t('{n} 项能力需在“能力中心”补配凭据', { n: needsSecret }));
          if (dropped) parts.push(t('{n} 项能力无法解析已跳过', { n: dropped }));
          message.warning(t('「{name}」已安装（{detail}）', { name: agent.name, detail: parts.join('，') }));
        } else {
          message.success(t('「{name}」已安装', { name: agent.name }));
        }
        markInstalled(agent.slug);
        onInstalled?.();
      } catch (error: unknown) {
        const ontologyFailure = getOntologyBuildFailure(error);
        if (ontologyFailure) {
          setBuildFailure(ontologyFailure);
        } else {
          message.error(error instanceof Error ? error.message : t('安装失败'));
        }
      } finally {
        setInstallingSlug(null);
      }
    },
    [fetchers, onInstalled, markInstalled],
  );

  const [deletingSlug, setDeletingSlug] = useState<string | null>(null);
  const doDelete = useCallback(
    async (agent: MarketplaceAgent) => {
      if (!fetchers.remove) return;
      setDeletingSlug(agent.slug);
      try {
        await fetchers.remove(agent.slug);
        message.success(t('「{name}」已从市场删除', { name: agent.name }));
        setDetail((prev) => (prev && prev.slug === agent.slug ? null : prev));
        await load();
      } catch (e) {
        message.error((e as Error).message || t('删除失败'));
      } finally {
        setDeletingSlug(null);
      }
    },
    [fetchers, load],
  );

  const [togglingSlug, setTogglingSlug] = useState<string | null>(null);
  const toggleEnabled = useCallback(
    async (agent: MarketplaceAgent, enabled: boolean) => {
      if (!fetchers.setEnabled) return;
      setTogglingSlug(agent.slug);
      try {
        await fetchers.setEnabled(agent.slug, enabled);
        setItems((prev) => prev.map((it) => (it.slug === agent.slug ? { ...it, market_enabled: enabled } : it)));
        setDetail((prev) => (prev && prev.slug === agent.slug ? { ...prev, market_enabled: enabled } : prev));
      } catch (e) {
        message.error((e as Error).message || t('操作失败'));
      } finally {
        setTogglingSlug(null);
      }
    },
    [fetchers],
  );

  const enableSwitch = (agent: MarketplaceAgent) =>
    fetchers.setEnabled ? (
      <Tooltip title={agent.market_enabled === false ? t('已下架，点击上架') : t('已上架，点击下架')}>
        <Switch
          size="small"
          checked={agent.market_enabled !== false}
          loading={togglingSlug === agent.slug}
          onClick={(_checked, e) => e.stopPropagation()}
          onChange={(v) => void toggleEnabled(agent, v)}
        />
      </Tooltip>
    ) : null;

  // Visibility scope (shown only when the admin injects visibility fetchers): a modal configures public/scoped + the authorization allowlist.
  const [visibilityTarget, setVisibilityTarget] = useState<MarketplaceAgent | null>(null);
  const onVisibilitySaved = useCallback((slug: string, visibility: MarketVisibilityValue) => {
    setItems((prev) => prev.map((it) => (it.slug === slug ? { ...it, visibility } : it)));
    setDetail((prev) => (prev && prev.slug === slug ? { ...prev, visibility } : prev));
  }, []);

  const visibilityButton = (agent: MarketplaceAgent) =>
    fetchers.visibility ? (
      <Tooltip title={agent.visibility === 'scoped' ? t('指定范围可见，点击调整') : t('所有人可见，点击设置可见范围')}>
        <Button
          type="text"
          size="small"
          icon={<EyeOutlined style={agent.visibility === 'scoped' ? { color: 'var(--color-warning)' } : undefined} />}
          onClick={(e) => { e.stopPropagation(); setVisibilityTarget(agent); }}
        />
      </Tooltip>
    ) : null;

  const deleteButton = (agent: MarketplaceAgent) =>
    fetchers.remove && agent.deletable ? (
      <Popconfirm
        title={t('确定从市场删除「{name}」？', { name: agent.name })}
        description={t('仅移出市场，不影响已安装的子智能体。')}
        okText={t('删除')}
        okButtonProps={{ danger: true }}
        cancelText={t('取消')}
        onConfirm={(e) => { e?.stopPropagation(); void doDelete(agent); }}
        onCancel={(e) => e?.stopPropagation()}
      >
        <Button type="text" size="small" danger icon={<DeleteOutlined />} loading={deletingSlug === agent.slug} onClick={(e) => e.stopPropagation()} />
      </Popconfirm>
    ) : null;

  const installButton = (agent: MarketplaceAgent, block?: boolean) => (
    <Button
      size="small"
      className="jx-mk-installBtn"
      type={agent.installed ? 'default' : 'primary'}
      disabled={agent.installed}
      block={block}
      loading={!agent.installed && installingSlug === agent.slug}
      onClick={(e) => { e.stopPropagation(); if (!agent.installed) void doInstall(agent); }}
    >
      <AnimatePresence mode="wait" initial={false}>
        {agent.installed ? (
          <motion.span key="installed" className="jx-mk-installBtnInner" initial={{ scale: 0.4, opacity: 0 }} animate={{ scale: 1, opacity: 1, transition: SPRING.pop }} exit={{ opacity: 0, transition: { duration: 0.1 } }}>
            <CheckOutlined /> {t('已安装')}
          </motion.span>
        ) : (
          <motion.span key="install" className="jx-mk-installBtnInner" exit={{ opacity: 0, transition: { duration: 0.1 } }}>
            {t('安装')}
          </motion.span>
        )}
      </AnimatePresence>
    </Button>
  );

  const q = query.trim().toLowerCase();
  const filtered = useMemo(() => {
    return items.filter((it) => {
      if (category !== '全部' && it.category !== category) return false;
      if (!q) return true;
      return `${it.name} ${it.summary} ${(it.tags || []).join(' ')} ${it.author}`.toLowerCase().includes(q);
    });
  }, [items, category, q]);

  const paged = useMemo(
    () => filtered.slice((page - 1) * MARKET_PAGE_SIZE, page * MARKET_PAGE_SIZE),
    [filtered, page],
  );

  useEffect(() => {
    const maxPage = Math.max(1, Math.ceil(filtered.length / MARKET_PAGE_SIZE));
    if (page > maxPage) setPage(1);
  }, [filtered.length, page]);

  const cats = useMemo(() => ['全部', ...categories], [categories]);

  return (
    <>
    <Modal
      title={detail ? (
        <span className="jx-mk-detailTitle">
          <Button type="text" size="small" icon={<LeftOutlined />} onClick={() => setDetail(null)} />
          {t('子智能体详情')}
        </span>
      ) : t('子智能体市场')}
      open={open}
      onCancel={onClose}
      footer={null}
      width={920}
      className="jx-mk-modal"
      destroyOnHidden
    >
      {detailLoading ? (
        <div style={{ padding: '48px 0', textAlign: 'center' }}><Spin /></div>
      ) : detail ? (
        <div className="jx-mk-detail">
          <div className="jx-mk-detailHead">
            <AgentMarketIcon avatar={detail.avatar} size={56} />
            <div className="jx-mk-detailHeadInfo">
              <div className="jx-mk-detailName">
                {detail.name}
                {detail.featured && <Tag color="orange" bordered={false} className="jx-mk-badge"><FireOutlined /> {t('精选')}</Tag>}
              </div>
              <div className="jx-mk-cardMeta">
                <Tag bordered={false} className="jx-mk-catTag">{detail.category}</Tag>
                {detail.source === 'community' && <Tag color="purple" bordered={false} className="jx-mk-badge">{t('社区共享 · {author}', { author: detail.author })}</Tag>}
                <span className="jx-mk-ver">v{detail.version}</span>
              </div>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 96 }}>
              {enableSwitch(detail)}
              {visibilityButton(detail)}
              {deleteButton(detail)}
              {installButton(detail, true)}
            </div>
          </div>

          {detail.summary && <p className="jx-mk-detailSummary">{detail.summary}</p>}

          {detail.tags?.length > 0 && (
            <div className="jx-mk-tags" style={{ marginBottom: 12 }}>
              {detail.tags.map((tag, i) => <Tag key={i} bordered={false} className="jx-mk-tag">{tag}</Tag>)}
            </div>
          )}

          <div className="jx-mk-detailBodyTitle">{t('携带能力')}</div>
          <div style={{ marginBottom: 12, fontSize: 13, color: 'var(--color-text-secondary)' }}>
            <CapabilityTags item={detail} />
            {detail.skill_count + detail.mcp_count + detail.plugin_count === 0 && (
              <span style={{ color: 'var(--color-text-tertiary)' }}>{t('纯提示词，无额外能力绑定')}</span>
            )}
          </div>

          {detail.suggested_questions?.length > 0 && (
            <>
              <div className="jx-mk-detailBodyTitle">{t('推荐问题')}</div>
              <div className="jx-mk-tags" style={{ marginBottom: 12 }}>
                {detail.suggested_questions.map((qq, i) => <Tag key={i} bordered={false} className="jx-mk-tag">{qq}</Tag>)}
              </div>
            </>
          )}

          <div className="jx-mk-detailBodyTitle">{t('角色设定（系统提示词）')}</div>
          <div className="jx-mk-detailBody jx-md" dangerouslySetInnerHTML={{ __html: mdToHtml(detail.system_prompt || '暂无内容') }} />
        </div>
      ) : (
        <>
          <div className="jx-mk-toolbar">
            <Input
              allowClear
              prefix={<SearchOutlined style={{ color: '#B3B3B3' }} />}
              placeholder={t('搜索子智能体名称、描述、标签')}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              style={{ maxWidth: 280 }}
            />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {t('安装后{scope}', { scope: scopeLabel || t('在本人「子智能体」中生成一个私有副本') })}
            </Typography.Text>
          </div>

          {cats.length > 1 && (
            <div className="jx-mk-cats">
              {cats.map((c) => (
                <button key={c} type="button" className={`jx-mk-catChip${category === c ? ' active' : ''}`} onClick={() => setCategory(c)}>
                  {category === c && (
                    <motion.span layoutId="agentMkCatChip" className="jx-mk-catChipBg" initial={false} transition={SPRING.ink} aria-hidden="true" />
                  )}
                  <span className="jx-mk-catChipLabel">{c}</span>
                </button>
              ))}
            </div>
          )}

          <Spin spinning={loading}>
            <div className="jx-mk-grid jx-anim-stagger" style={{ '--stagger-step': '30ms' } as React.CSSProperties} key={`agentmk-${page}-${category}`}>
              {paged.map((agent, idx) => (
                <div key={agent.slug} className="jx-mk-card jx-card-lift" style={staggerStyle(idx)} onClick={() => void openDetail(agent.slug)}>
                  <div className="jx-mk-cardTop">
                    <AgentMarketIcon avatar={agent.avatar} />
                    <div className="jx-mk-cardHead">
                      <div className="jx-mk-cardNameRow">
                        <span className="jx-mk-cardName" title={agent.name}>{agent.name}</span>
                        {agent.featured && <Tag color="orange" bordered={false} className="jx-mk-badge"><FireOutlined /> {t('精选')}</Tag>}
                      </div>
                      <div className="jx-mk-cardMeta">
                        <Tag bordered={false} className="jx-mk-catTag">{agent.category}</Tag>
                        {agent.market_enabled === false && <Tag color="default" bordered={false}>{t('已下架')}</Tag>}
                        {fetchers.visibility && agent.visibility === 'scoped' && <Tag color="gold" bordered={false}>{t('指定可见')}</Tag>}
                        {agent.source === 'community' && <Tag color="purple" bordered={false} className="jx-mk-badge">{t('社区')}</Tag>}
                      </div>
                    </div>
                  </div>
                  <div className="jx-mk-cardDesc" title={agent.summary}>{agent.summary || '—'}</div>
                  <div className="jx-mk-cardFoot">
                    <CapabilityTags item={agent} />
                    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                      {enableSwitch(agent)}
                      {visibilityButton(agent)}
                      {deleteButton(agent)}
                      {installButton(agent)}
                    </span>
                  </div>
                </div>
              ))}
            </div>
            {!loading && filtered.length === 0 && (
              <Empty className="jx-anim-fadeIn" description={t('没有匹配的子智能体')} style={{ padding: '32px 0' }} />
            )}
            {filtered.length > MARKET_PAGE_SIZE && (
              <div style={{ display: 'flex', justifyContent: 'center', marginTop: 16 }}>
                <Pagination
                  current={page}
                  pageSize={MARKET_PAGE_SIZE}
                  total={filtered.length}
                  onChange={setPage}
                  showSizeChanger={false}
                  showTotal={(n) => t('共 {n} 个子智能体', { n })}
                  size="small"
                />
              </div>
            )}
          </Spin>
        </>
      )}
    </Modal>

    {/* Visibility-scope configuration modal (reachable only when the admin injects visibility fetchers) */}
    {fetchers.visibility && (
      <VisibilityScopeModal
        open={!!visibilityTarget}
        slug={visibilityTarget?.slug || null}
        itemName={visibilityTarget?.name}
        fetchers={fetchers.visibility}
        onClose={() => setVisibilityTarget(null)}
        onSaved={onVisibilitySaved}
      />
    )}
    <OntologyBuildValidationModal failure={buildFailure} onClose={() => setBuildFailure(null)} />
    </>
  );
}
