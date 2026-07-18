import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { motion } from 'motion/react';
import { Switch, Tag, Input, Typography, Button, Modal, Form, Popconfirm, message, Empty, Spin, Dropdown, Alert } from 'antd';
import {
  SearchOutlined, LeftOutlined, DeleteOutlined, AppstoreOutlined, PlusOutlined, DownOutlined,
  AppstoreAddOutlined, UploadOutlined, ApiOutlined, BulbOutlined, CheckCircleOutlined, WarningOutlined, StopOutlined,
} from '@ant-design/icons';
import { t } from '../../i18n';
import { useCatalogStore, useAuthStore, usePluginStore } from '../../stores';
import { mdToHtml } from '../../utils/markdown';
import { staggerStyle } from '../../utils/motionTokens';
import { DRILL_IN_BACK, DRILL_IN_DETAIL } from '../../utils/motionVariants';
import { usePanelHeader } from '../../hooks/usePageConfig';
import { DingTalkConnect } from '../settings/DingTalkConnect';
import { LarkConnect } from '../settings/LarkConnect';
import { EmailConnect } from '../settings/EmailConnect';
import { YidaConnect } from '../settings/YidaConnect';
import {
  listPlugins, listInstalledPlugins, getInstalledPluginDetail,
  installPlugin, importPlugin, uninstallPlugin, setPluginEnabled,
} from '../../api';
import type {
  PluginListItem, InstalledPluginItem, InstalledPluginDetail,
  PluginImportReport, PluginRequiredSecret, PluginSkillComponent, PluginMcpComponent,
} from '../../types';

interface PluginsPageProps {
  embedded?: boolean;
  onDetailChange?: (detail: boolean) => void;
}

type Level = 'list' | 'plugin' | 'component';
type SelectedComponent =
  | { kind: 'skill'; data: PluginSkillComponent }
  | { kind: 'mcp'; data: PluginMcpComponent };

function normSecret(s: string | PluginRequiredSecret): PluginRequiredSecret {
  return typeof s === 'string' ? { key: s, label: s, required: true } : s;
}

function PluginIcon({ size = 36 }: { size?: number }) {
  return (
    <div className="jx-mcp-iconWrap jx-mcp-iconFallback" style={{ width: size, height: size }}>
      <AppstoreOutlined style={{ color: '#6366f1' }} />
    </div>
  );
}

function sourceLabel(source?: string): string | null {
  if (source === 'imported_claude') return 'Claude Code';
  if (source === 'imported_codex') return 'Codex';
  return null;
}

export function PluginsPage({ onDetailChange }: PluginsPageProps) {
  const fetchCatalog = useCatalogStore((s) => s.fetchCatalog);
  const { manageQuery, setManageQuery, panelEntryNonce } = useCatalogStore();
  const canImportPlugin = useAuthStore((s) => s.authUser?.can_import_plugin === true);
  const { title, subtitle } = usePanelHeader('plugins', {
    title: '插件库',
    subtitle: '插件把成套的技能与 MCP 工具打包成一个整体，安装后即可整组启用。',
  });

  const [loading, setLoading] = useState(false);
  const [market, setMarket] = useState<PluginListItem[]>([]);
  const [installed, setInstalled] = useState<InstalledPluginItem[]>([]);
  const [busySlug, setBusySlug] = useState<string | null>(null);
  const [searchVisible, setSearchVisible] = useState(false);
  const [marketOpen, setMarketOpen] = useState(false);
  const [navDir, setNavDir] = useState<'detail' | 'list' | null>(null);

  // drill-in：list → plugin → component
  const [level, setLevel] = useState<Level>('list');
  const [pluginDetail, setPluginDetail] = useState<InstalledPluginDetail | null>(null);
  const [component, setComponent] = useState<SelectedComponent | null>(null);
  const [toolDetail, setToolDetail] = useState<{ name: string; description?: string } | null>(null);

  // Credentials / import report modals
  const [secretPlugin, setSecretPlugin] = useState<{ slug: string; name: string; secrets: PluginRequiredSecret[] } | null>(null);
  const [secretForm] = Form.useForm();
  const [report, setReport] = useState<{ name: string; report: PluginImportReport } | null>(null);
  const importInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => { onDetailChange?.(level !== 'list'); }, [level, onDetailChange]);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [m, ins] = await Promise.all([listPlugins(), listInstalledPlugins()]);
      setMarket(m);
      setInstalled(ins);
    } catch (e) {
      message.error((e as Error).message || t('加载插件失败'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  // List refresh and catalog refresh are independent, run in parallel (used after install/uninstall).
  // Also force-refresh the shared pluginStore so the chat input's "+" / "/" menus sync immediately.
  const afterChange = useCallback(async () => {
    await Promise.all([
      refresh(),
      fetchCatalog().catch(() => {}),
      usePluginStore.getState().fetchInstalled(true),
    ]);
  }, [refresh, fetchCatalog]);

  // ── Navigation ──
  const openInstalled = useCallback(async (installId: string) => {
    try {
      const d = await getInstalledPluginDetail(installId);
      setPluginDetail(d);
      setNavDir('detail');
      setLevel('plugin');
    } catch (e) { message.error((e as Error).message || t('加载详情失败')); }
  }, []);

  const openComponent = useCallback((c: SelectedComponent) => {
    setComponent(c);
    setNavDir('detail');
    setLevel('component');
  }, []);

  const backToList = useCallback(() => {
    setNavDir('list');
    setLevel('list');
    setPluginDetail(null);
  }, []);

  const backToPlugin = useCallback(() => {
    setNavDir('list');
    setLevel('plugin');
    setComponent(null);
  }, []);

  // ── Install / import / uninstall / toggle ──
  const doInstall = useCallback(async (slug: string, name: string, secrets: Record<string, string>) => {
    setBusySlug(slug);
    try {
      const res = await installPlugin(slug, secrets);
      message.success(t('插件已安装：{name}', { name }));
      setReport({ name, report: res.import_report });
      await afterChange();
    } catch (e) {
      message.error((e as Error).message || t('安装失败'));
    } finally {
      setBusySlug(null);
    }
  }, [afterChange]);

  const handleInstall = useCallback((p: PluginListItem) => {
    const secrets = (p.required_secrets || []).map(normSecret);
    if (secrets.length > 0) {
      setSecretPlugin({ slug: p.slug, name: p.name, secrets });
      secretForm.resetFields();
      return;
    }
    void doInstall(p.slug, p.name, {});
  }, [secretForm, doInstall]);

  const submitSecretInstall = useCallback(async () => {
    if (!secretPlugin) return;
    const values = await secretForm.validateFields().catch(() => null);
    if (!values) return;
    await doInstall(secretPlugin.slug, secretPlugin.name, values as Record<string, string>);
    setSecretPlugin(null);
  }, [secretPlugin, secretForm, doInstall]);

  const handleImportFile = useCallback(async (file: File) => {
    setLoading(true);
    try {
      const res = await importPlugin(file, {});
      message.success(t('已导入插件：{name}', { name: res.name }));
      setReport({ name: res.name, report: res.import_report });
      await afterChange();
    } catch (e) {
      message.error((e as Error).message || t('导入失败'));
    } finally {
      setLoading(false);
    }
  }, [afterChange]);

  const handleUninstall = useCallback(async (installId: string, name: string) => {
    try {
      await uninstallPlugin(installId);
      message.success(t('已卸载：{name}', { name }));
      // When uninstalling from the detail page, return to the list; when uninstalling directly from a list card, stay on the list (avoid a pointless back animation).
      if (level !== 'list') backToList();
      await afterChange();
    } catch (e) { message.error((e as Error).message || t('卸载失败')); }
  }, [afterChange, backToList, level]);

  const handleToggle = useCallback(async (installId: string, enabled: boolean) => {
    try {
      await setPluginEnabled(installId, enabled);
      // Refresh the list (the installed item's enabled flag changed) + catalog + shared store; also refresh the detail when it's open.
      await afterChange();
      if (pluginDetail && pluginDetail.install_id === installId) {
        const d = await getInstalledPluginDetail(installId).catch(() => null);
        if (d) setPluginDetail(d);
      }
    } catch (e) { message.error((e as Error).message || t('操作失败')); }
  }, [afterChange, pluginDetail]);

  // ── Filter ──
  const query = manageQuery.trim().toLowerCase();
  const installedSlugs = useMemo(() => new Set(installed.map((p) => p.slug)), [installed]);
  const matchText = (s: string) => !query || s.toLowerCase().includes(query);
  const shownInstalled = useMemo(
    () => installed.filter((p) => matchText(`${p.name} ${p.description} ${p.slug}`)),
    [installed, query],
  );
  const shownMarket = useMemo(
    () => market.filter((p) => !installedSlugs.has(p.slug) && matchText(`${p.name} ${p.description} ${p.slug} ${p.category}`)),
    [market, installedSlugs, query],
  );
  // The "plugin library" header count only reflects installed plugins (in-library cards); the plugin marketplace is in a modal and not counted.
  const totalCount = installed.length;

  // ════════════ Tool detail (fourth level): full-page display like the skill detail ════════════
  if (toolDetail) {
    return (
      <motion.div key="tool" className="jx-mcp-detailPage" {...(navDir === 'detail' ? DRILL_IN_DETAIL : { initial: false })}>
        <div className="jx-mcp-stickyHeader">
          <button className="jx-mcp-backBtn jx-mcp-backBtn--inline" onClick={() => setToolDetail(null)}>
            <LeftOutlined style={{ fontSize: 14 }} />
          </button>
          <div className="jx-mcp-iconWrap jx-mcp-iconFallback">
            <ApiOutlined style={{ color: '#22c55e' }} />
          </div>
          <span className="jx-mcp-detailName">{toolDetail.name}</span>
          <div style={{ flex: 1 }} />
          <Tag>{t('工具')}</Tag>
        </div>
        <div className="jx-mcp-stickyBody">
          <div className="jx-sk-metaCard">
            <p className="jx-sk-metaDesc" style={{ whiteSpace: 'pre-wrap' }}>
              {toolDetail.description || t('暂无描述')}
            </p>
          </div>
        </div>
      </motion.div>
    );
  }

  // ════════════ Component detail (third level) ════════════
  if (level === 'component' && component) {
    const isSkill = component.kind === 'skill';
    const name = component.data.name;
    return (
      <motion.div key="component" className="jx-mcp-detailPage" {...(navDir === 'detail' ? DRILL_IN_DETAIL : { initial: false })}>
        <div className="jx-mcp-stickyHeader">
          <button className="jx-mcp-backBtn jx-mcp-backBtn--inline" onClick={backToPlugin}>
            <LeftOutlined style={{ fontSize: 14 }} />
          </button>
          <div className="jx-mcp-iconWrap jx-mcp-iconFallback">
            {isSkill ? <BulbOutlined style={{ color: '#f59e0b' }} /> : <ApiOutlined style={{ color: '#22c55e' }} />}
          </div>
          <span className="jx-mcp-detailName">{name}</span>
          <Tag style={component.data.enabled
            ? { background: '#DBE9FF', color: '#126DFF', border: 'none' }
            : { background: '#F5F6F7', color: '#B3B3B3', border: 'none' }}>
            {component.data.enabled ? t('已启用') : t('未启用')}
          </Tag>
          <div style={{ flex: 1 }} />
          <Tag>{isSkill ? t('技能') : 'MCP'}</Tag>
        </div>

        <div className="jx-mcp-stickyBody">
          <div className="jx-sk-metaCard">
            <p className="jx-sk-metaDesc">{component.data.description || t('暂无描述')}</p>
          </div>

          {component.kind === 'skill' ? (
            (() => {
              const sk = component.data;
              return (
                <div className="jx-mcp-detailBody">
                  {sk.instructions ? (
                    <div className="jx-md jx-mcp-detailMarkdown"
                      dangerouslySetInnerHTML={{ __html: mdToHtml(sk.instructions) }} />
                  ) : <Typography.Text type="secondary">{t('暂无指令内容')}</Typography.Text>}
                  {sk.files.length > 0 && (
                    <div style={{ marginTop: 16 }}>
                      <h4>{t('附带文件')}</h4>
                      <ul style={{ paddingLeft: 20, color: '#4b5563', fontSize: 13 }}>
                        {sk.files.map((f) => <li key={f}>{f}</li>)}
                      </ul>
                    </div>
                  )}
                </div>
              );
            })()
          ) : (
            (() => {
              const mc = component.data;
              return (
                <div className="jx-mcp-detailBody">
                  <h4>{t('工具列表')}（{mc.tools.length}）</h4>
                  {mc.needs_runtime && (
                    <Typography.Paragraph type="warning" style={{ fontSize: 13 }}>
                      {t('该 MCP 为 stdio 类型，需运行时环境，已安装但默认禁用。')}
                    </Typography.Paragraph>
                  )}
                  {mc.tools.length === 0 ? (
                    <Typography.Text type="secondary">{t('暂未发现工具（可能尚未连接）')}</Typography.Text>
                  ) : (
                    <div className="jx-mcp-grid" style={{ marginTop: 8 }}>
                      {mc.tools.map((tool) => (
                        <div key={tool.name} className="jx-mcp-card jx-card-lift" style={{ cursor: 'pointer' }}
                          onClick={() => setToolDetail(tool)}>
                          <div className="jx-mcp-cardTop">
                            <ApiOutlined style={{ color: '#22c55e' }} />
                            <span className="jx-mcp-cardName" style={{ marginLeft: 8 }}>{tool.name}</span>
                          </div>
                          <div className="jx-mcp-cardDesc">{tool.description || t('暂无描述')}</div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })()
          )}
        </div>
      </motion.div>
    );
  }

  // ════════════ Plugin detail (second level, only installed plugins can enter) ════════════
  if (level === 'plugin' && pluginDetail) {
    const d = pluginDetail;
    const skills: PluginSkillComponent[] = d.skills;
    const mcps: PluginMcpComponent[] = d.mcp;
    const srcLabel = sourceLabel(d.source);
    const dropped = d.import_report?.dropped;
    const isInstalled = true;

    return (
      <motion.div key="plugin" className="jx-mcp-detailPage" {...(navDir === 'detail' ? DRILL_IN_DETAIL : { initial: false })}>
        <div className="jx-mcp-stickyHeader">
          <button className="jx-mcp-backBtn jx-mcp-backBtn--inline" onClick={backToList}>
            <LeftOutlined style={{ fontSize: 14 }} />
          </button>
          <PluginIcon size={28} />
          <span className="jx-mcp-detailName">{d.name}</span>
          <span className="jx-mcp-version" style={{ marginLeft: 4 }}>v{d.version}</span>
          {srcLabel && <Tag color="purple" style={{ marginLeft: 6 }}>{srcLabel}</Tag>}
          <div style={{ flex: 1 }} />
          {d.is_global && <Tag color="gold" style={{ marginRight: 8 }}>{t('管理员')}</Tag>}
          <span className="jx-mcp-enableLabel">{t('启用')}</span>
          <Switch
            checked={d.skills.some((s) => s.enabled) || d.mcp.some((m) => m.enabled)}
            onChange={(v) => void handleToggle(d.install_id, v)}
            style={{ marginRight: 8 }}
          />
          {/* Global plugins are managed by the admin; users can't uninstall them (only disable for themselves) */}
          {!d.is_global && (
            <Popconfirm title={t('确定卸载该插件？其技能与 MCP 将一并移除')}
              onConfirm={() => void handleUninstall(d.install_id, d.name)}>
              <Button danger size="small" icon={<DeleteOutlined />}>{t('卸载')}</Button>
            </Popconfirm>
          )}
        </div>

        <div className="jx-mcp-stickyBody">
          <div className="jx-sk-metaCard">
            <h4 className="jx-sk-metaName">{d.name}</h4>
            <p className="jx-sk-metaDesc">{d.description || t('暂无描述')}</p>
            {d.category && <Tag>{d.category}</Tag>}
          </div>

          {/* Account connection (per-user OAuth device flow): when a plugin declares a connection, the one-time authorization is completed here.
              It used to be under "Settings → Integrations"; now it's consolidated into the corresponding plugin detail page. */}
          {d.connection && (
            <div style={{ marginTop: 12 }}>
              <h4 className="jx-sectionTitle">{t('账号连接')}</h4>
              <div className="jx-settings-card">
                {d.connection === 'dingtalk' && <DingTalkConnect />}
                {d.connection === 'lark' && <LarkConnect />}
                {d.connection === 'email' && <EmailConnect />}
                {d.connection === 'yida' && <YidaConnect />}
              </div>
            </div>
          )}

          {/* Admin config (read-only on the user side: only see whether it's provisioned, can't modify; if not configured, prompt to contact the admin) */}
          {d.admin_config && (
            <div style={{ marginTop: 12 }}>
              <h4 className="jx-sectionTitle">{t('管理员配置')}</h4>
              {d.admin_config.configured ? (
                <Alert type="success" showIcon
                  message={t('该插件已由管理员配置，可直接使用。')} />
              ) : (
                <Alert type="warning" showIcon
                  message={t('该插件需要管理员配置后才能使用')}
                  description={t('请联系管理员在「插件库」中为本插件开通相关配置（{items}）。', {
                    items: d.admin_config.fields.map((f) => f.label).join('、'),
                  })} />
              )}
            </div>
          )}

          {/* Skill components */}
          <div style={{ marginTop: 12 }}>
            <h4 className="jx-sectionTitle">{t('技能')}（{skills.length}）</h4>
            {skills.length === 0 ? <Typography.Text type="secondary">{t('该插件不含技能')}</Typography.Text> : (
              <div className="jx-mcp-grid">
                {skills.map((s, idx) => (
                  <ComponentCard
                    key={s.skill_id || s.name}
                    idx={idx}
                    icon={<BulbOutlined style={{ color: '#f59e0b' }} />}
                    name={s.name}
                    desc={s.description || t('点击查看详情')}
                    onClick={() => openComponent({ kind: 'skill', data: s })}
                    tags={isInstalled && (
                      <Tag style={{ marginLeft: 8, ...(s.enabled
                        ? { background: '#DBE9FF', color: '#126DFF', border: 'none' }
                        : { background: '#F5F6F7', color: '#B3B3B3', border: 'none' }) }}>
                        {s.enabled ? t('已启用') : t('未启用')}
                      </Tag>
                    )}
                  />
                ))}
              </div>
            )}
          </div>

          {/* MCP components */}
          {mcps.length > 0 && (
            <div style={{ marginTop: 16 }}>
              <h4 className="jx-sectionTitle">{t('MCP 工具')}（{mcps.length}）</h4>
              <div className="jx-mcp-grid">
                {mcps.map((m, idx) => (
                  <ComponentCard
                    key={m.server_id || m.name}
                    idx={idx}
                    icon={<ApiOutlined style={{ color: '#22c55e' }} />}
                    name={m.name}
                    desc={isInstalled ? t('{n} 个工具', { n: m.tools?.length ?? 0 }) : (m.note || t('点击查看详情'))}
                    onClick={() => openComponent({ kind: 'mcp', data: m })}
                    tags={<>
                      <Tag style={{ marginLeft: 8 }}>{m.transport === 'stdio' ? 'stdio' : '远程'}</Tag>
                      {m.needs_runtime && <Tag color="orange" style={{ marginLeft: 4 }}>{t('需运行时')}</Tag>}
                    </>}
                  />
                ))}
              </div>
            </div>
          )}

          {/* Not-imported components */}
          {dropped && dropped.length > 0 && (
            <div style={{ marginTop: 16 }}>
              <h4 className="jx-sectionTitle">{t('未导入组件')}（{dropped.length}）</h4>
              <ul style={{ paddingLeft: 20, color: '#9ca3af', fontSize: 13 }}>
                {dropped.map((x, i) => <li key={i}>{x.type}/{x.name} — {x.reason}</li>)}
              </ul>
            </div>
          )}
        </div>
      </motion.div>
    );
  }

  // ════════════ List (first level) ════════════
  return (
    <motion.div key="list" className="jx-mcp-page" {...(navDir === 'list' ? DRILL_IN_BACK : { initial: false })}>
      <div className="jx-mcp-header">
        <div>
          <h2 className="jx-mcp-title">
            {title}
            <span className="jx-sectionTitleCount">{t('（共 {n} 项）', { n: totalCount })}</span>
          </h2>
          {subtitle ? <p className="jx-mcp-subtitle">{subtitle}</p> : null}
        </div>
        <div className="jx-mcp-headerRight">
          {searchVisible ? (
            <Input allowClear placeholder={t('搜索插件关键词')} className="jx-mcp-searchInput"
              value={manageQuery} onChange={(e) => setManageQuery(e.target.value)} autoFocus
              prefix={<SearchOutlined style={{ color: '#B3B3B3' }} />}
              onBlur={() => { if (!manageQuery) setSearchVisible(false); }} />
          ) : (
            <div className="jx-mcp-searchBox" onClick={() => setSearchVisible(true)}>
              <SearchOutlined style={{ color: '#B3B3B3', fontSize: 14 }} />
              <span className="jx-mcp-searchPlaceholder">{t('搜索插件关键词')}</span>
            </div>
          )}
          {canImportPlugin && (
            <>
              <input ref={importInputRef} type="file" accept=".zip" style={{ display: 'none' }}
                onChange={(e) => { const f = e.target.files?.[0]; if (f) void handleImportFile(f); e.target.value = ''; }} />
              <Dropdown
                menu={{ items: [
                  { key: 'market', icon: <AppstoreAddOutlined />, label: t('从插件市场获取'), onClick: () => setMarketOpen(true) },
                  { key: 'upload', icon: <UploadOutlined />, label: t('导入插件包（zip）'), onClick: () => importInputRef.current?.click() },
                ] }}
              >
                <Button type="primary" icon={<PlusOutlined />} style={{ marginLeft: 8 }}>
                  {t('添加插件')} <DownOutlined />
                </Button>
              </Dropdown>
            </>
          )}
        </div>
      </div>

      <Spin spinning={loading}>
        {/* Installed plugins (card grid consistent with the skill library / MCP library). The plugin marketplace is moved into a modal, opened per permission. */}
        {shownInstalled.length === 0 ? (
          <Empty
            description={canImportPlugin ? t('还没有安装插件，点击右上角「添加插件」从插件市场获取') : t('还没有可用插件')}
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            style={{ marginTop: 40 }}
          />
        ) : (
          <div className="jx-mcp-grid jx-anim-stagger" style={{ '--stagger-step': '30ms' } as React.CSSProperties}
            key={`pi-${panelEntryNonce}`}>
            {shownInstalled.map((p, idx) => (
              <div key={p.install_id} className="jx-mcp-card jx-card-lift" style={staggerStyle(idx)}
                onClick={() => void openInstalled(p.install_id)}>
                <div className="jx-mcp-cardTop">
                  <PluginIcon />
                  <div className="jx-mcp-cardNameGroup">
                    <span className="jx-mcp-cardName">{p.name}</span>
                    {p.is_global && <Tag color="gold">{t('管理员')}</Tag>}
                    {sourceLabel(p.source) && <Tag color="purple">{sourceLabel(p.source)}</Tag>}
                  </div>
                  {/* Users can enable/disable for themselves (global plugins are also a personal switch, not affecting each other);
                      non-global plugins can be uninstalled directly on the card, without entering the detail page first. */}
                  <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 4 }}
                    onClick={(e) => e.stopPropagation()}>
                    <Switch size="small" checked={p.enabled !== false}
                      onChange={(v) => void handleToggle(p.install_id, v)}
                      checkedChildren={t('启用')} unCheckedChildren={t('停用')} />
                    {!p.is_global && (
                      <Popconfirm title={t('确定卸载该插件？其技能与 MCP 将一并移除')}
                        okText={t('卸载')} cancelText={t('取消')} okButtonProps={{ danger: true }}
                        onConfirm={() => void handleUninstall(p.install_id, p.name)}>
                        <Button type="text" size="small" danger icon={<DeleteOutlined />}
                          title={t('卸载插件')} onClick={(e) => e.stopPropagation()} />
                      </Popconfirm>
                    )}
                  </div>
                </div>
                <div className="jx-mcp-cardDesc">{p.description}</div>
              </div>
            ))}
          </div>
        )}
      </Spin>

      {/* Plugin marketplace modal (only openable by those with permission; modeled on the skill library's skill marketplace) */}
      <Modal open={marketOpen} title={t('插件市场')} onCancel={() => setMarketOpen(false)}
        footer={null} width={760} styles={{ body: { maxHeight: '62vh', overflow: 'auto' } }}>
        {shownMarket.length === 0 ? (
          <Empty description={t('插件市场暂无可安装的插件')} image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          <div className="jx-mcp-grid">
            {shownMarket.map((p) => (
              <div key={p.slug} className="jx-mcp-card">
                <div className="jx-mcp-cardTop">
                  <PluginIcon />
                  <div className="jx-mcp-cardNameGroup">
                    <span className="jx-mcp-cardName">{p.name}</span>
                    {p.category && <Tag>{p.category}</Tag>}
                  </div>
                  <Button type="primary" size="small" loading={busySlug === p.slug}
                    style={{ marginLeft: 'auto' }} onClick={() => void handleInstall(p)}>{t('安装')}</Button>
                </div>
                <div className="jx-mcp-cardDesc">{p.description}</div>
                <div style={{ marginTop: 6, display: 'flex', gap: 6 }}>
                  <Tag color="blue">{t('技能')} {p.skills_count}</Tag>
                  {(p.required_secrets || []).length > 0 && <Tag color="orange">{t('需凭据')}</Tag>}
                </div>
              </div>
            ))}
          </div>
        )}
      </Modal>

      {/* Credentials modal */}
      <Modal open={!!secretPlugin} title={t('配置凭据：{name}', { name: secretPlugin?.name || '' })}
        onCancel={() => setSecretPlugin(null)} onOk={() => void submitSecretInstall()}
        confirmLoading={!!busySlug} okText={t('安装')} destroyOnClose>
        <Form form={secretForm} layout="vertical">
          {(secretPlugin?.secrets || []).map((f) => (
            <Form.Item key={f.key} name={f.key} label={f.label || f.key}
              rules={f.required ? [{ required: true, message: t('请输入 {label}', { label: f.label || f.key }) }] : []}>
              <Input.Password autoComplete="off" placeholder={t('请输入 {label}', { label: f.label || f.key })} />
            </Form.Item>
          ))}
        </Form>
      </Modal>

      {/* Import report modal */}
      <Modal open={!!report} title={t('导入报告：{name}', { name: report?.name || '' })}
        onCancel={() => setReport(null)}
        footer={<Button type="primary" onClick={() => setReport(null)}>{t('知道了')}</Button>} width={560}>
        {report && <ImportReportView report={report.report} />}
      </Modal>
    </motion.div>
  );
}

// Skill/MCP component card inside plugin details (shared card skeleton; tags are injected by the caller per type).
function ComponentCard({ idx, icon, name, tags, desc, onClick }: {
  idx: number;
  icon: React.ReactNode;
  name: string;
  tags?: React.ReactNode;
  desc: React.ReactNode;
  onClick: () => void;
}) {
  return (
    <div className="jx-mcp-card jx-card-lift" style={staggerStyle(idx)} onClick={onClick}>
      <div className="jx-mcp-cardTop">
        {icon}
        <span className="jx-mcp-cardName" style={{ marginLeft: 8 }}>{name}</span>
        {tags}
      </div>
      <div className="jx-mcp-cardDesc">{desc}</div>
    </div>
  );
}

function ImportReportView({ report }: { report: PluginImportReport }) {
  return (
    <div>
      <ReportGroup icon={<CheckCircleOutlined style={{ color: '#22c55e' }} />} title={t('已导入')}
        rows={(report.imported || []).map((x) => `${x.type === 'skill' ? t('技能') : 'MCP'}：${x.name}`)} />
      <ReportGroup icon={<WarningOutlined style={{ color: '#f59e0b' }} />} title={t('已适配（降级）')}
        rows={(report.adapted || []).map((x) => `${x.name}：${x.note || t('已装上但默认禁用')}`)} />
      <ReportGroup icon={<StopOutlined style={{ color: '#ef4444' }} />} title={t('已丢弃（本平台不支持）')}
        rows={(report.dropped || []).map((x) => `${x.type}/${x.name}：${x.reason}`)} />
    </div>
  );
}

function ReportGroup({ icon, title, rows }: { icon: React.ReactNode; title: string; rows: string[] }) {
  if (rows.length === 0) return null;
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ fontWeight: 600, marginBottom: 6, display: 'flex', alignItems: 'center', gap: 6 }}>{icon}{title}（{rows.length}）</div>
      <ul style={{ margin: 0, paddingLeft: 22, color: '#4b5563', fontSize: 13 }}>
        {rows.map((r, i) => <li key={i}>{r}</li>)}
      </ul>
    </div>
  );
}
