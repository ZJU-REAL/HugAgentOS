import { useCallback, useEffect, useState } from 'react';
import {
  Button, Empty, Form, Input, Modal, Popconfirm, Select, Table, Tabs, Tag, message,
} from 'antd';
import {
  ArrowLeftOutlined,
  AppstoreOutlined,
  DeleteOutlined,
  EditOutlined,
  EyeOutlined,
  GlobalOutlined,
  LinkOutlined,
  LockOutlined,
  SearchOutlined,
  SettingOutlined,
} from '@ant-design/icons';
import {
  clearSiteKv,
  clearSiteSubmissions,
  deleteSite,
  deleteSiteKvKey,
  exportSiteSubmissions,
  getSiteDetail,
  listSiteKv,
  listSiteSubmissions,
  listSites,
  rollbackSite,
  updateSite,
  type SiteItem,
  type SiteKvItem,
  type SiteSubmissionItem,
  type SiteVersionItem,
} from '../../api';
import {
  EditionSiteVisibilityFields,
  EditionSiteVisibilityTag,
  editionSiteFormValues,
  editionSiteUpdateFields,
  getSiteVisibilityOptions,
  type SiteVisibility,
} from '../../editionSiteVisibility';
import { useCatalogStore } from '../../stores';
import { useChatStore } from '../../stores/chatStore';
import { usePluginStore } from '../../stores/pluginStore';
import { t } from '../../i18n';
import '../../styles/sites.css';

interface SitesPanelProps {
  onBack: () => void;
}

/** Enter a "site" building session in the main chat: reuse the main chat input (with attachments/projects/+ menu),
 *  and auto-activate the installed "site" plugin (injecting the site-builder skill + site_publish tool). Site-building
 *  is purely plugin-gated — if not installed, guide the user to Capability Center → plugin install rather than forcing
 *  into a session that has no publish tool. */
async function ensureSitesPluginInstalled(): Promise<boolean> {
  // First ensure the installed-plugin list is up to date (it may have just been installed/uninstalled elsewhere).
  await usePluginStore.getState().fetchInstalled(true).catch(() => {});
  const installed = usePluginStore
    .getState()
    .installed.some((p) => p.slug === 'sites' && p.enabled !== false);
  if (!installed) {
    message.info(t('站点建站由「站点」插件提供，请先在能力中心 → 插件里安装后再创建'));
    useCatalogStore.getState().setPanel('ability_center');
    return false;
  }
  return true;
}

async function startSiteCreation() {
  if (!(await ensureSitesPluginInstalled())) return;
  // Don't pre-create the project: on publish the backend automatically creates a source project named after the site
  // title and drops the files into it (see internal_sites), avoiding placeholder directory names like "Site · Building"
  // and not depending on frontend/agent project binding.
  useChatStore.getState().enterSiteMode();
  useCatalogStore.getState().setPanel('chat');
}

/** Open an "edit" session for a published site: bind its source project, and the agent edits inside the project folder and republishes. */
async function startSiteEdit(site: SiteItem) {
  if (!site.project_id) {
    message.info(t('该站点是旧版本、没有源码工程，无法在线编辑（可新建一个站点替代）'));
    return;
  }
  if (!(await ensureSitesPluginInstalled())) return;
  useChatStore.getState().enterSiteMode({
    projectId: site.project_id,
    projectName: site.title,
    title: site.title,
  });
  useCatalogStore.getState().setPanel('chat');
}

function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${bytes} B`;
}

function formatTime(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function VisibilityTag({ site }: { site: SiteItem }) {
  if (site.visibility === 'public') {
    return <Tag icon={<GlobalOutlined />} color="blue">{t('公开')}</Tag>;
  }
  if (site.visibility !== 'private') {
    return <EditionSiteVisibilityTag visibility={site.visibility} />;
  }
  return <Tag icon={<LockOutlined />}>{t('私密')}</Tag>;
}

/** Site management modal: Settings / version rollback / form data / KV storage */
function SiteManageModal({
  site, onClose, onChanged,
}: {
  site: SiteItem;
  onClose: () => void;
  onChanged: (updated: SiteItem) => void;
}) {
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [visibility, setVisibility] = useState<SiteVisibility>(site.visibility);
  const [versions, setVersions] = useState<SiteVersionItem[]>([]);
  const [currentVersion, setCurrentVersion] = useState(site.current_version);
  const [rollingBack, setRollingBack] = useState<number | null>(null);
  const [submissions, setSubmissions] = useState<SiteSubmissionItem[]>([]);
  const [submissionTotal, setSubmissionTotal] = useState(0);
  const [kvItems, setKvItems] = useState<SiteKvItem[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    form.setFieldsValue({
      title: site.title, slug: site.slug,
      visibility: site.visibility,
      ...editionSiteFormValues(site),
    });
    void getSiteDetail(site.site_id)
      .then((d) => { setVersions([...d.versions].reverse()); setCurrentVersion(d.current_version); })
      .catch(() => {});
    void listSiteSubmissions(site.site_id, 1, 50)
      .then((r) => { setSubmissions(r.items); setSubmissionTotal(r.total); })
      .catch(() => {});
    void listSiteKv(site.site_id).then((r) => setKvItems(r.items)).catch(() => {});
  }, [site, form]);

  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      setSaving(true);
      const updated = await updateSite(site.site_id, {
        title: values.title,
        slug: values.slug !== site.slug ? values.slug : undefined,
        visibility: values.visibility,
        ...editionSiteUpdateFields(values.visibility, values),
      });
      onChanged(updated);
      message.success(t('已保存'));
      onClose();
    } catch (e) {
      if (e instanceof Error) message.error(t('保存失败：') + e.message);
    } finally {
      setSaving(false);
    }
  };

  const handleRollback = async (version: number) => {
    setRollingBack(version);
    try {
      const updated = await rollbackSite(site.site_id, version);
      setCurrentVersion(updated.current_version);
      onChanged(updated);
      message.success(t('已回滚到版本') + ` v${version}`);
    } catch (e) {
      message.error(t('回滚失败：') + (e as Error).message);
    } finally {
      setRollingBack(null);
    }
  };

  const handleExport = async () => {
    setBusy(true);
    try {
      const r = await exportSiteSubmissions(site.site_id);
      message.success(t('已导出到「我的空间」：') + r.filename);
      window.open(r.download_url, '_blank', 'noopener,noreferrer');
    } catch (e) {
      message.error(t('导出失败：') + (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const handleClearSubmissions = async () => {
    setBusy(true);
    try {
      await clearSiteSubmissions(site.site_id);
      setSubmissions([]); setSubmissionTotal(0);
      message.success(t('表单数据已清空'));
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const handleDeleteKv = async (key: string) => {
    try {
      await deleteSiteKvKey(site.site_id, key);
      setKvItems((prev) => prev.filter((i) => i.key !== key));
    } catch (e) {
      message.error((e as Error).message);
    }
  };

  const handleClearKv = async () => {
    try {
      await clearSiteKv(site.site_id);
      setKvItems([]);
      message.success(t('KV 已清空'));
    } catch (e) {
      message.error((e as Error).message);
    }
  };

  const submissionColumns = [
    { title: t('时间'), dataIndex: 'created_at', width: 160, render: (v: string | null) => formatTime(v) },
    { title: t('表单'), dataIndex: 'form_key', width: 110 },
    {
      title: t('内容'), dataIndex: 'payload',
      render: (p: Record<string, unknown>) => (
        <span className="jx-sites-payload">{JSON.stringify(p, null, 0)}</span>
      ),
    },
  ];

  const kvColumns = [
    { title: 'Key', dataIndex: 'key', width: 160 },
    {
      title: 'Value', dataIndex: 'value',
      render: (v: string) => <span className="jx-sites-payload">{v}</span>,
    },
    { title: t('更新于'), dataIndex: 'updated_at', width: 160, render: (v: string | null) => formatTime(v) },
    {
      title: '', key: 'op', width: 60,
      render: (_: unknown, row: SiteKvItem) => (
        <Button size="small" type="text" danger icon={<DeleteOutlined />} onClick={() => handleDeleteKv(row.key)} />
      ),
    },
  ];

  return (
    <Modal
      title={`${t('站点管理')} — ${site.title}`}
      open
      onCancel={onClose}
      footer={null}
      width={720}
      destroyOnClose
    >
      <Tabs
        items={[
          {
            key: 'settings',
            label: t('设置'),
            children: (
              <Form form={form} layout="vertical">
                <Form.Item name="title" label={t('站点标题')} rules={[{ required: true, message: t('请输入站点标题') }]}>
                  <Input maxLength={200} />
                </Form.Item>
                <Form.Item
                  name="slug"
                  label={t('访问地址')}
                  extra={t('仅支持 3-50 位小写字母、数字、连字符；修改后旧链接会失效')}
                  rules={[
                    { required: true, message: t('请输入访问地址') },
                    { pattern: /^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$/, message: t('格式不正确') },
                  ]}
                >
                  <Input addonBefore={`${window.location.origin}/site/`} addonAfter="/" />
                </Form.Item>
                <Form.Item name="visibility" label={t('可见性')}>
                  <Select
                    onChange={(v) => setVisibility(v)}
                    options={getSiteVisibilityOptions()}
                  />
                </Form.Item>
                <EditionSiteVisibilityFields visibility={visibility} />
                <Button type="primary" onClick={handleSave} loading={saving}>{t('保存')}</Button>
              </Form>
            ),
          },
          {
            key: 'versions',
            label: `${t('版本')} (${versions.length})`,
            children: (
              <Table
                size="small"
                rowKey="version"
                pagination={false}
                dataSource={versions}
                columns={[
                  {
                    title: t('版本'), dataIndex: 'version', width: 100,
                    render: (v: number) => (
                      <span>
                        v{v}{' '}
                        {v === currentVersion ? <Tag color="green">{t('当前线上')}</Tag> : null}
                      </span>
                    ),
                  },
                  { title: t('发布时间'), dataIndex: 'created_at', render: (v: string) => formatTime(v) },
                  { title: t('文件数'), dataIndex: 'file_count', width: 90 },
                  { title: t('大小'), dataIndex: 'total_size_bytes', width: 100, render: (v: number) => formatSize(v) },
                  {
                    title: '', key: 'op', width: 100,
                    render: (_: unknown, row: SiteVersionItem) =>
                      row.version === currentVersion ? null : (
                        <Popconfirm
                          title={t('回滚站点')}
                          description={t('线上内容将立即切换到该版本，确定回滚？')}
                          okText={t('回滚')}
                          cancelText={t('取消')}
                          onConfirm={() => handleRollback(row.version)}
                        >
                          <Button size="small" loading={rollingBack === row.version}>{t('回滚')}</Button>
                        </Popconfirm>
                      ),
                  },
                ]}
              />
            ),
          },
          {
            key: 'submissions',
            label: `${t('表单数据')} (${submissionTotal})`,
            children: (
              <div>
                <div className="jx-sites-tabActions">
                  <Button size="small" type="primary" onClick={handleExport} loading={busy} disabled={!submissionTotal}>
                    {t('导出 CSV 到我的空间')}
                  </Button>
                  <Popconfirm
                    title={t('清空全部表单数据？')}
                    okText={t('清空')}
                    okButtonProps={{ danger: true }}
                    cancelText={t('取消')}
                    onConfirm={handleClearSubmissions}
                  >
                    <Button size="small" danger disabled={!submissionTotal}>{t('清空')}</Button>
                  </Popconfirm>
                </div>
                <Table
                  size="small"
                  rowKey="id"
                  pagination={{ pageSize: 8 }}
                  dataSource={submissions}
                  columns={submissionColumns}
                  locale={{ emptyText: t('站点表单提交后会出现在这里') }}
                />
              </div>
            ),
          },
          {
            key: 'kv',
            label: `KV (${kvItems.length})`,
            children: (
              <div>
                <div className="jx-sites-tabActions">
                  <Popconfirm
                    title={t('清空全部 KV 数据？')}
                    okText={t('清空')}
                    okButtonProps={{ danger: true }}
                    cancelText={t('取消')}
                    onConfirm={handleClearKv}
                  >
                    <Button size="small" danger disabled={!kvItems.length}>{t('清空')}</Button>
                  </Popconfirm>
                </div>
                <Table
                  size="small"
                  rowKey="key"
                  pagination={{ pageSize: 8 }}
                  dataSource={kvItems}
                  columns={kvColumns}
                  locale={{ emptyText: t('站点通过 __api/kv 写入的数据会出现在这里') }}
                />
              </div>
            ),
          },
        ]}
      />
    </Modal>
  );
}

export function SitesPanel({ onBack }: SitesPanelProps) {
  const [sites, setSites] = useState<SiteItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [managing, setManaging] = useState<SiteItem | null>(null);
  const [keyword, setKeyword] = useState('');

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const { items } = await listSites();
      setSites(items);
    } catch (e) {
      message.error(t('加载站点列表失败：') + (e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const siteFullUrl = (site: SiteItem) => `${window.location.origin}${site.url}`;

  const handleCopy = async (site: SiteItem) => {
    try {
      await navigator.clipboard.writeText(siteFullUrl(site));
      message.success(t('链接已复制'));
    } catch {
      message.error(t('复制失败，请手动复制'));
    }
  };

  const handleDelete = async (site: SiteItem) => {
    try {
      await deleteSite(site.site_id);
      setSites((prev) => prev.filter((s) => s.site_id !== site.site_id));
      message.success(t('站点已删除'));
    } catch (e) {
      message.error(t('删除失败：') + (e as Error).message);
    }
  };

  const kw = keyword.trim().toLowerCase();
  const filteredSites = kw
    ? sites.filter(
        (s) => s.title.toLowerCase().includes(kw) || s.slug.toLowerCase().includes(kw),
      )
    : sites;

  // ── My sites list / management view (the build entry is in the main chat; clicking "Create" jumps to a main-chat building session) ──
  return (
    <div className="jx-agentPage">
      <div className="jx-agentPage-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <Button type="text" icon={<ArrowLeftOutlined />} onClick={onBack} style={{ marginRight: 4 }} />
          <div>
            <div className="jx-agentPage-title">{t('站点')}</div>
            <div className="jx-agentPage-subtitle">{t('将你的想法变成真实网站')}</div>
          </div>
        </div>
        <Button type="primary" onClick={startSiteCreation}>{t('创建')}</Button>
      </div>

      <div className="jx-sites-body">
        <Input
          allowClear
          className="jx-sites-search"
          prefix={<SearchOutlined style={{ color: 'var(--color-text-placeholder)' }} />}
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          placeholder={t('搜索站点')}
        />

        {!loading && sites.length === 0 ? (
          <Empty
            image={<AppstoreOutlined style={{ fontSize: 44, opacity: 0.35 }} />}
            description={<div className="jx-sites-emptyTitle">{t('暂无站点')}</div>}
            style={{ marginTop: 80 }}
          >
            <Button onClick={startSiteCreation}>{t('创建新站点')}</Button>
          </Empty>
        ) : (
          <div className="jx-sites-list">
            {filteredSites.map((site) => (
              <div key={site.site_id} className="jx-sites-card jx-card-lift">
                <div className="jx-sites-cardMain">
                  <div className="jx-sites-cardHead">
                    <span className="jx-sites-cardTitle">{site.title}</span>
                    <VisibilityTag site={site} />
                  </div>
                  <a className="jx-sites-cardUrl" href={site.url} target="_blank" rel="noopener noreferrer">
                    {siteFullUrl(site)}
                  </a>
                  <div className="jx-sites-cardMeta">
                    {t('版本')} v{site.current_version} · {site.file_count} {t('个文件')} ·{' '}
                    {formatSize(site.total_size_bytes)} · <EyeOutlined /> {site.view_count} {t('次访问')}
                    {site.updated_at ? ` · ${t('更新于')} ${formatTime(site.updated_at)}` : ''}
                  </div>
                </div>
                <div className="jx-sites-cardActions">
                  <Button
                    size="small"
                    type="primary"
                    ghost
                    onClick={() => window.open(site.url, '_blank', 'noopener,noreferrer')}
                  >
                    {t('打开')}
                  </Button>
                  <Button size="small" icon={<LinkOutlined />} onClick={() => handleCopy(site)}>
                    {t('复制链接')}
                  </Button>
                  {site.editable ? (
                    <Button size="small" icon={<EditOutlined />} onClick={() => startSiteEdit(site)}>
                      {t('编辑')}
                    </Button>
                  ) : (
                    <Button
                      size="small"
                      icon={<EditOutlined />}
                      disabled
                      title={t('该站点没有源码工程，无法在线编辑')}
                    >
                      {t('编辑')}
                    </Button>
                  )}
                  <Button size="small" icon={<SettingOutlined />} onClick={() => setManaging(site)}>
                    {t('管理')}
                  </Button>
                  <Popconfirm
                    title={t('删除站点')}
                    description={t('删除后访问地址将立即失效，且不可恢复。确定删除？')}
                    okText={t('删除')}
                    okButtonProps={{ danger: true }}
                    cancelText={t('取消')}
                    onConfirm={() => handleDelete(site)}
                  >
                    <Button size="small" danger icon={<DeleteOutlined />}>{t('删除')}</Button>
                  </Popconfirm>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {managing ? (
        <SiteManageModal
          site={managing}
          onClose={() => setManaging(null)}
          onChanged={(updated) =>
            setSites((prev) => prev.map((s) => (s.site_id === updated.site_id ? updated : s)))
          }
        />
      ) : null}
    </div>
  );
}
