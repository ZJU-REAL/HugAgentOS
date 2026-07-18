import { useCallback, useEffect, useMemo, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { Modal, Input, Tag, Button, Empty, Spin, Form, Typography, Pagination, Tooltip, Popconfirm, Switch, message } from 'antd';
import { t } from '../../i18n';
import { SearchOutlined, FireOutlined, DownloadOutlined, KeyOutlined, CheckOutlined, LeftOutlined, FileTextOutlined, DeleteOutlined, EyeOutlined } from '@ant-design/icons';
import type { MarketplaceSkill, MarketplaceSkillDetail, MarketplaceFetchers, MarketVisibilityValue } from '../../types';
import { mdToHtml } from '../../utils/markdown';
import { SPRING, staggerStyle } from '../../utils/motionTokens';
import { VisibilityScopeModal } from '../common';
import { SkillAvatar, categoryPreset } from './skillIcons';

// Skill marketplace modal: browse preset skills, view details and install. The transport (user / admin) is injected via fetchers,
// and the same component is reused in both the capability center and /admin skill management. The icon prefers icon_url, otherwise a built-in icon by category.
function MarketIcon({ skill, size }: { skill: MarketplaceSkill; size?: number }) {
  return <SkillAvatar icon={skill.icon_url || categoryPreset(skill.category)} seed={skill.slug} size={size || 40} />;
}

// List page size (2-column grid × 6 rows)
const MARKET_PAGE_SIZE = 12;

export function SkillMarketplaceModal({
  open,
  onClose,
  fetchers,
  onInstalled,
  scopeLabel,
}: {
  open: boolean;
  onClose: () => void;
  fetchers: MarketplaceFetchers;
  onInstalled?: (
    result: { id: string; action?: string },
    skill: MarketplaceSkill,
  ) => void;
  scopeLabel?: string;
}) {
  const [loading, setLoading] = useState(false);
  const [items, setItems] = useState<MarketplaceSkill[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [query, setQuery] = useState('');
  const [category, setCategory] = useState<string>('全部');
  const [page, setPage] = useState(1);
  const [installingSlug, setInstallingSlug] = useState<string | null>(null);

  // Detail view
  const [detail, setDetail] = useState<MarketplaceSkillDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // Credential configuration modal
  const [secretSkill, setSecretSkill] = useState<MarketplaceSkill | null>(null);
  const [secretForm] = Form.useForm();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchers.loadList();
      setItems(res.items || []);
      setCategories(res.categories || []);
    } catch (e) {
      message.error((e as Error).message || t('加载技能市场失败'));
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

  // Return to the first page when searching / switching category
  useEffect(() => {
    setPage(1);
  }, [query, category]);

  const openDetail = useCallback(async (slug: string) => {
    setDetailLoading(true);
    setDetail(null);
    try {
      setDetail(await fetchers.loadDetail(slug));
    } catch (e) {
      message.error((e as Error).message || t('加载技能详情失败'));
    } finally {
      setDetailLoading(false);
    }
  }, [fetchers]);

  const markInstalled = useCallback((slug: string, depStatus: 'installing' | 'ready' | 'rejected' = 'ready') => {
    setItems((prev) => prev.map((it) => (it.slug === slug ? { ...it, installed: true, dep_status: depStatus } : it)));
    setDetail((prev) => (prev && prev.slug === slug ? { ...prev, installed: true, dep_status: depStatus } : prev));
  }, []);

  const doInstall = useCallback(
    async (skill: MarketplaceSkill, secrets: Record<string, string>) => {
      setInstallingSlug(skill.slug);
      try {
        const r = await fetchers.install(skill.slug, secrets);
        const depPending = (r as { dep_pending?: boolean }).dep_pending;
        if (depPending) {
          message.success(t('「{name}」已安装，检测到需额外依赖，已通知管理员安装，装好后即可使用', { name: skill.display_name }));
        } else {
          message.success(r.action === 'updated' ? t('「{name}」已更新', { name: skill.display_name }) : t('「{name}」已安装', { name: skill.display_name }));
        }
        markInstalled(skill.slug, depPending ? 'installing' : 'ready');
        onInstalled?.(r, skill);
      } catch (e) {
        message.error((e as Error).message || t('安装失败'));
      } finally {
        setInstallingSlug(null);
      }
    },
    [fetchers, onInstalled, markInstalled],
  );

  const [deletingSlug, setDeletingSlug] = useState<string | null>(null);
  const doDelete = useCallback(
    async (skill: MarketplaceSkill) => {
      if (!fetchers.remove) return;
      setDeletingSlug(skill.slug);
      try {
        await fetchers.remove(skill.slug);
        message.success(t('「{name}」已从技能市场删除', { name: skill.display_name }));
        setDetail((prev) => (prev && prev.slug === skill.slug ? null : prev));
        await load();
      } catch (e) {
        message.error((e as Error).message || t('删除失败'));
      } finally {
        setDeletingSlug(null);
      }
    },
    [fetchers, load],
  );

  // List/delist toggle (shown only when the admin injects setEnabled): updates in place to avoid list re-fetch jitter.
  const [togglingSlug, setTogglingSlug] = useState<string | null>(null);
  const toggleEnabled = useCallback(
    async (skill: MarketplaceSkill, enabled: boolean) => {
      if (!fetchers.setEnabled) return;
      setTogglingSlug(skill.slug);
      try {
        await fetchers.setEnabled(skill.slug, enabled);
        setItems((prev) => prev.map((it) => (it.slug === skill.slug ? { ...it, market_enabled: enabled } : it)));
        setDetail((prev) => (prev && prev.slug === skill.slug ? { ...prev, market_enabled: enabled } : prev));
      } catch (e) {
        message.error((e as Error).message || t('操作失败'));
      } finally {
        setTogglingSlug(null);
      }
    },
    [fetchers],
  );

  // List toggle: shown only when a setEnabled fetcher (admin) is injected. Delisted = not shown on the user side.
  const enableSwitch = (skill: MarketplaceSkill) =>
    fetchers.setEnabled ? (
      <Tooltip title={skill.market_enabled === false ? t('已下架，点击上架') : t('已上架，点击下架')}>
        <Switch
          size="small"
          checked={skill.market_enabled !== false}
          loading={togglingSlug === skill.slug}
          onClick={(_checked, e) => e.stopPropagation()}
          onChange={(v) => void toggleEnabled(skill, v)}
        />
      </Tooltip>
    ) : null;

  // Visibility scope (shown only when the admin injects visibility fetchers): modal configures public/scoped + authorization whitelist.
  const [visibilityTarget, setVisibilityTarget] = useState<MarketplaceSkill | null>(null);
  const onVisibilitySaved = useCallback((slug: string, visibility: MarketVisibilityValue) => {
    setItems((prev) => prev.map((it) => (it.slug === slug ? { ...it, visibility } : it)));
    setDetail((prev) => (prev && prev.slug === slug ? { ...prev, visibility } : prev));
  }, []);

  const visibilityButton = (skill: MarketplaceSkill) =>
    fetchers.visibility ? (
      <Tooltip title={skill.visibility === 'scoped' ? t('指定范围可见，点击调整') : t('所有人可见，点击设置可见范围')}>
        <Button
          type="text"
          size="small"
          icon={<EyeOutlined style={skill.visibility === 'scoped' ? { color: 'var(--color-warning)' } : undefined} />}
          onClick={(e) => { e.stopPropagation(); setVisibilityTarget(skill); }}
        />
      </Tooltip>
    ) : null;

  // Delete button: shown only when a remove fetcher (admin) is injected and the item is a DB listing record (deletable).
  const deleteButton = (skill: MarketplaceSkill) =>
    fetchers.remove && skill.deletable ? (
      <Popconfirm
        title={t('确定从技能市场删除「{name}」？', { name: skill.display_name })}
        description={t('仅移出市场，不影响已安装的技能实例。')}
        okText={t('删除')}
        okButtonProps={{ danger: true }}
        cancelText={t('取消')}
        onConfirm={(e) => { e?.stopPropagation(); void doDelete(skill); }}
        onCancel={(e) => e?.stopPropagation()}
      >
        <Button
          type="text"
          size="small"
          danger
          icon={<DeleteOutlined />}
          loading={deletingSlug === skill.slug}
          onClick={(e) => e.stopPropagation()}
        />
      </Popconfirm>
    ) : null;

  const handleInstallClick = useCallback(
    (skill: MarketplaceSkill) => {
      if (skill.required_secrets && skill.required_secrets.length > 0) {
        secretForm.resetFields();
        setSecretSkill(skill);
        return;
      }
      void doInstall(skill, {});
    },
    [doInstall, secretForm],
  );

  const submitSecrets = useCallback(async () => {
    if (!secretSkill) return;
    const values = await secretForm.validateFields();
    const secrets: Record<string, string> = {};
    for (const f of secretSkill.required_secrets) {
      const v = (values[f.key] || '').trim();
      if (v) secrets[f.key] = v;
    }
    setSecretSkill(null);
    await doInstall(secretSkill, secrets);
  }, [secretSkill, secretForm, doInstall]);

  const q = query.trim().toLowerCase();
  const filtered = useMemo(() => {
    return items.filter((it) => {
      if (category !== '全部' && it.category !== category) return false;
      if (!q) return true;
      return `${it.display_name} ${it.summary} ${it.tags.join(' ')} ${it.author}`.toLowerCase().includes(q);
    });
  }, [items, category, q]);

  const paged = useMemo(
    () => filtered.slice((page - 1) * MARKET_PAGE_SIZE, page * MARKET_PAGE_SIZE),
    [filtered, page],
  );

  // Pull back to the first page when data changes push the page number out of range
  useEffect(() => {
    const maxPage = Math.max(1, Math.ceil(filtered.length / MARKET_PAGE_SIZE));
    if (page > maxPage) setPage(1);
  }, [filtered.length, page]);

  const cats = useMemo(() => ['全部', ...categories], [categories]);

  // Install button (reused in list card / detail page): three states install → loading → installed.
  // A single Button swaps props (rather than hard-switching two Buttons), the background color goes through a CSS transition,
  // the text hands off via AnimatePresence mode="wait", and the installed-state Check icon springs in.
  const installButton = (skill: MarketplaceSkill, block?: boolean) => {
    const btn = (
    <Button
      size="small"
      className="jx-mk-installBtn"
      type={skill.installed ? 'default' : 'primary'}
      danger={skill.installed && skill.dep_status === 'rejected'}
      disabled={skill.installed}
      block={block}
      loading={!skill.installed && installingSlug === skill.slug}
      onClick={(e) => { e.stopPropagation(); if (!skill.installed) handleInstallClick(skill); }}
    >
      <AnimatePresence mode="wait" initial={false}>
        {skill.installed ? (
          <motion.span
            key="installed"
            className="jx-mk-installBtnInner"
            initial={{ scale: 0.4, opacity: 0 }}
            animate={{ scale: 1, opacity: 1, transition: SPRING.pop }}
            exit={{ opacity: 0, transition: { duration: 0.1 } }}
          >
            <CheckOutlined /> {skill.builtin
              ? t('已内置')
              : skill.dep_status === 'installing'
                ? t('依赖安装中')
                : skill.dep_status === 'rejected'
                  ? t('管理员未通过')
                  : t('已安装')}
          </motion.span>
        ) : (
          <motion.span
            key="install"
            className="jx-mk-installBtnInner"
            exit={{ opacity: 0, transition: { duration: 0.1 } }}
          >
            {t('安装')}
          </motion.span>
        )}
      </AnimatePresence>
    </Button>
    );
    if (skill.installed && skill.dep_status === 'rejected') {
      return (
        <Tooltip title={skill.dep_reason
          ? t('管理员未通过：{reason}', { reason: skill.dep_reason })
          : t('管理员未通过该技能的依赖安装申请')}>
          {btn}
        </Tooltip>
      );
    }
    return btn;
  };

  return (
    <>
      <Modal
        title={detail ? (
          <span className="jx-mk-detailTitle">
            <Button type="text" size="small" icon={<LeftOutlined />} onClick={() => setDetail(null)} />
            {t('技能详情')}
          </span>
        ) : t('技能市场')}
        open={open}
        onCancel={onClose}
        footer={null}
        width={920}
        className="jx-mk-modal"
        destroyOnHidden
      >
        {/* ── Detail view ── */}
        {detailLoading ? (
          <div style={{ padding: '48px 0', textAlign: 'center' }}><Spin /></div>
        ) : detail ? (
          <div className="jx-mk-detail">
            <div className="jx-mk-detailHead">
              <MarketIcon skill={detail} size={56} />
              <div className="jx-mk-detailHeadInfo">
                <div className="jx-mk-detailName">
                  {detail.display_name}
                  {detail.featured && <Tag color="orange" bordered={false} className="jx-mk-badge"><FireOutlined /> {t('精选')}</Tag>}
                </div>
                <div className="jx-mk-cardMeta">
                  <Tag bordered={false} className="jx-mk-catTag">{detail.category}</Tag>
                  {detail.builtin ? (
                    <Tag color="blue" bordered={false} className="jx-mk-badge">{t('内置')}</Tag>
                  ) : detail.source === 'community' ? (
                    <Tag color="purple" bordered={false} className="jx-mk-badge">{t('社区共享 · {author}', { author: detail.author })}</Tag>
                  ) : (
                    <span className="jx-mk-dl"><DownloadOutlined /> {detail.downloads}</span>
                  )}
                  <span className="jx-mk-ver">v{detail.version}</span>
                  {detail.requires_api_key && <span className="jx-mk-keyHint"><KeyOutlined /> {t('需配置凭据')}</span>}
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
                {detail.tags.map((t, i) => <Tag key={i} bordered={false} className="jx-mk-tag">{t}</Tag>)}
              </div>
            )}

            {detail.required_secrets?.length > 0 && (
              <div className="jx-mk-secretsNote">
                <KeyOutlined /> 安装时需配置：{detail.required_secrets.map((s) => s.label || s.key).join('、')}
              </div>
            )}

            <div className="jx-mk-detailBodyTitle">{t('技能说明（SKILL.md）')}</div>
            <div className="jx-mk-detailBody jx-md" dangerouslySetInnerHTML={{ __html: mdToHtml(detail.instructions || '暂无内容') }} />

            {detail.files?.length > 0 && (
              <>
                <div className="jx-mk-detailBodyTitle">{t('附带文件（{n}）', { n: detail.files.length })}</div>
                <div className="jx-mk-files">
                  {detail.files.map((f) => (
                    <div key={f.path} className="jx-mk-file">
                      <FileTextOutlined /> <span className="jx-mk-filePath">{f.path}</span>
                      <span className="jx-mk-fileSize">{(f.size / 1024).toFixed(1)} KB</span>
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>
        ) : (
          /* ── List view ── */
          <>
            <div className="jx-mk-toolbar">
              <Input
                allowClear
                prefix={<SearchOutlined style={{ color: '#B3B3B3' }} />}
                placeholder={t('搜索技能名称、描述、标签')}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                style={{ maxWidth: 280 }}
              />
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {t('安装后{scope}', { scope: scopeLabel || t('仅自己可见可用') })}
              </Typography.Text>
            </div>

            {cats.length > 1 && (
              <div className="jx-mk-cats">
                {cats.map((c) => (
                  <button
                    key={c}
                    type="button"
                    className={`jx-mk-catChip${category === c ? ' active' : ''}`}
                    onClick={() => setCategory(c)}
                  >
                    {category === c && (
                      <motion.span
                        layoutId="mkCatChip"
                        className="jx-mk-catChipBg"
                        initial={false}
                        transition={SPRING.ink}
                        aria-hidden="true"
                      />
                    )}
                    <span className="jx-mk-catChipLabel">{c}</span>
                  </button>
                ))}
              </div>
            )}

            <Spin spinning={loading}>
              <div
                className="jx-mk-grid jx-anim-stagger"
                style={{ '--stagger-step': '30ms' } as React.CSSProperties}
                key={`mk-${page}-${category}`}
              >
                {paged.map((skill, idx) => (
                  <div
                    key={skill.slug}
                    className="jx-mk-card jx-card-lift"
                    style={staggerStyle(idx)}
                    onClick={() => void openDetail(skill.slug)}
                  >
                    <div className="jx-mk-cardTop">
                      <MarketIcon skill={skill} />
                      <div className="jx-mk-cardHead">
                        <div className="jx-mk-cardNameRow">
                          <span className="jx-mk-cardName" title={skill.display_name}>{skill.display_name}</span>
                          {skill.featured && <Tag color="orange" bordered={false} className="jx-mk-badge"><FireOutlined /> {t('精选')}</Tag>}
                        </div>
                        <div className="jx-mk-cardMeta">
                          <Tag bordered={false} className="jx-mk-catTag">{skill.category}</Tag>
                          {skill.market_enabled === false && <Tag color="default" bordered={false}>{t('已下架')}</Tag>}
                          {fetchers.visibility && skill.visibility === 'scoped' && <Tag color="gold" bordered={false}>{t('指定可见')}</Tag>}
                          {skill.builtin ? (
                            <Tag color="blue" bordered={false} className="jx-mk-badge">{t('内置')}</Tag>
                          ) : skill.source === 'community' ? (
                            <Tag color="purple" bordered={false} className="jx-mk-badge">{t('社区')}</Tag>
                          ) : (
                            <span className="jx-mk-dl"><DownloadOutlined /> {skill.downloads}</span>
                          )}
                          {skill.requires_api_key && <span className="jx-mk-keyHint"><KeyOutlined /> {t('需配置')}</span>}
                        </div>
                      </div>
                    </div>
                    <div className="jx-mk-cardDesc" title={skill.summary}>{skill.summary || '—'}</div>
                    <div className="jx-mk-cardFoot">
                      <div className="jx-mk-tags">
                        {(skill.tags || []).slice(0, 3).map((t, i) => <Tag key={i} bordered={false} className="jx-mk-tag">{t}</Tag>)}
                      </div>
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                        {enableSwitch(skill)}
                        {visibilityButton(skill)}
                        {deleteButton(skill)}
                        {installButton(skill)}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
              {!loading && filtered.length === 0 && (
                <Empty className="jx-anim-fadeIn" description={t('没有匹配的技能')} style={{ padding: '32px 0' }} />
              )}
              {filtered.length > MARKET_PAGE_SIZE && (
                <div style={{ display: 'flex', justifyContent: 'center', marginTop: 16 }}>
                  <Pagination
                    current={page}
                    pageSize={MARKET_PAGE_SIZE}
                    total={filtered.length}
                    onChange={setPage}
                    showSizeChanger={false}
                    showTotal={(n) => t('共 {n} 个技能', { n })}
                    size="small"
                  />
                </div>
              )}
            </Spin>
          </>
        )}
      </Modal>

      {/* Visibility scope configuration modal (reachable only when the admin injects visibility fetchers) */}
      {fetchers.visibility && (
        <VisibilityScopeModal
          open={!!visibilityTarget}
          slug={visibilityTarget?.slug || null}
          itemName={visibilityTarget?.display_name}
          fetchers={fetchers.visibility}
          onClose={() => setVisibilityTarget(null)}
          onSaved={onVisibilitySaved}
        />
      )}

      {/* Credential configuration modal (pops up when installing a skill that needs an API-Key etc.) */}
      <Modal
        title={secretSkill ? t('配置「{name}」', { name: secretSkill.display_name }) : t('配置凭据')}
        open={!!secretSkill}
        onCancel={() => setSecretSkill(null)}
        onOk={() => void submitSecrets()}
        okText={t('安装')}
        cancelText={t('取消')}
        confirmLoading={!!secretSkill && installingSlug === secretSkill.slug}
        destroyOnHidden
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          {t('该技能运行需要以下凭据。凭据仅保存在你安装的这份技能里，不会上传到技能市场。')}
        </Typography.Paragraph>
        <Form form={secretForm} layout="vertical">
          {secretSkill?.required_secrets.map((f) => (
            <Form.Item
              key={f.key}
              name={f.key}
              label={f.label || f.key}
              tooltip={f.help}
              extra={f.help}
              rules={f.required ? [{ required: true, message: t('请填写{label}', { label: f.label || f.key }) }] : []}
            >
              <Input.Password placeholder={f.placeholder || t('请输入 {label}', { label: f.label || f.key })} autoComplete="off" />
            </Form.Item>
          ))}
        </Form>
      </Modal>
    </>
  );
}
