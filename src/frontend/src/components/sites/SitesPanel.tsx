import { useCallback, useEffect, useState } from 'react';
import {
  Alert, Button, Empty, Input, Popconfirm, Skeleton, Tag, message,
} from 'antd';
import {
  AppstoreOutlined,
  ArrowLeftOutlined,
  DeleteOutlined,
  EditOutlined,
  ExportOutlined,
  EyeOutlined,
  GlobalOutlined,
  LinkOutlined,
  LockOutlined,
  SearchOutlined,
  SettingOutlined,
} from '@ant-design/icons';
import {
  deleteSite,
  listSites,
  getProject,
  getSession,
  isHybridDual,
  isLocalProject,
  prepareLocalSiteProject,
  openLocalSiteEditor,
  type SiteItem,
} from '../../api';
import {
  EditionSiteVisibilityTag,
} from '../../editionSiteVisibility';
import { HostedResourceCard } from './HostedResourceCard';
import { ApplicationCard, type ApplicationView } from './ApplicationCard';
import { ApplicationManageModal } from './ApplicationManageModal';
import { useApplications } from './useApplications';
import { isMcpApplication, type Application } from './applicationApi';
import { ensureSitesPluginInstalled, startMcpEdit } from './hostedResourceEditor';
import { SitePasswordTag } from './SitePasswordField';
import { useIsMobileViewport } from '../../hooks/useIsMobileViewport';
import { useCatalogStore } from '../../stores';
import { useChatStore } from '../../stores/chatStore';
import { stablePublicOrigin } from '../../stores/deploymentModeStore';
import { copyToClipboard } from '../../utils/clipboard';
import { pickSiteEditChat } from '../../utils/history';
import { t } from '../../i18n';
import { formatDate } from '../../utils/date';
import { SiteManageModal } from './SiteManageModal';
import { formatSize } from './siteFormatting';
import '../../styles/sites.css';

/** Enter a "site" building session in the main chat: reuse the main chat input (with attachments/projects/+ menu),
 *  and auto-activate the installed "site" plugin (injecting the site-builder skill + site_publish tool). Site-building
 *  is purely plugin-gated — if not installed, guide the user to Capability Center → plugin install rather than forcing
 *  into a session that has no publish tool. */

async function startSiteCreation() {
  if (!(await ensureSitesPluginInstalled())) return;
  if (isHybridDual()) {
    try {
      const state = useChatStore.getState();
      const projectId = state.store.chats[state.currentChatId]?.projectId;
      const project = await prepareLocalSiteProject(isLocalProject(projectId) ? projectId : undefined);
      state.enterSiteMode({projectId: project.project_id, projectName: project.project_name});
    } catch (error) {
      message.error(error instanceof Error ? error.message : t('操作失败'));
      return;
    }
  } else {
    useChatStore.getState().enterSiteMode();
  }
  useCatalogStore.getState().setPanel('chat');
}

/** Open an "edit" session for a published site: bind its source project, and the agent edits inside the project folder and republishes. */
async function startSiteEdit(site: SiteItem) {
  if (!(await ensureSitesPluginInstalled())) return;

  if (site.local_source) {
    try {
      const source = await openLocalSiteEditor(site.site_id);
      const original = await getSession(source.chat_id);
      const state = useChatStore.getState();
      state.updateStore((store) => ({
        ...store,
        chats: { ...store.chats, [original.id]: {
          ...original, ...store.chats[original.id], siteChat: true,
          projectId: source.project_id, projectName: source.project_name, runTarget: 'local',
        } },
        order: store.order.includes(original.id) ? store.order : [original.id, ...store.order],
      }));
      state.setCurrentChatId(original.id);
      useCatalogStore.getState().setPanel('chat');
    } catch (error) {
      message.error(error instanceof Error ? error.message : t('操作失败'));
    }
    return;
  }

  if (!site.project_id) {
    message.info(t('该站点是旧版本、没有源码工程，无法在线编辑（可新建一个站点替代）'));
    return;
  }

  // 源码工程可能已经被用户删掉了，而站点表里的 project_id 还留着。照旧绑上去，
  // 侧边栏会拿 chat.projectName 兜底造出一个「已删除项目」的分组，新对话就挂在
  // 一个并不存在的项目下（刷新后才消失）。所以先确认工程还在。
  try {
    await getProject(site.project_id);
  } catch {
    message.info(t('该站点的源码工程已被删除，无法在线编辑（可新建一个站点替代）'));
    return;
  }

  // 先找这个站点已有的建站会话：站点是从某段对话里建出来的，「编辑」理应回到那段
  // 对话继续改，而不是每点一次就开一个空白新对话（挑选规则见 pickSiteEditChat）。
  const { store, setCurrentChatId } = useChatStore.getState();
  const existing = pickSiteEditChat(Object.values(store.chats), site.project_id);

  if (existing) {
    setCurrentChatId(existing.id);
  } else {
    useChatStore.getState().enterSiteMode({
      projectId: site.project_id,
      projectName: site.title,
      title: site.title,
    });
  }
  useCatalogStore.getState().setPanel('chat');
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

export function SitesPanel() {
  const applications = useApplications();
  const [editingApplication, setEditingApplication] = useState<string | null>(null);
  const [activeApplication, setActiveApplication] = useState<{ app: Application; view: ApplicationView } | null>(null);
  const [sites, setSites] = useState<SiteItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [siteError, setSiteError] = useState('');
  const [managing, setManaging] = useState<SiteItem | null>(null);
  const [keyword, setKeyword] = useState('');
  // 手机上「打开站点」不能是 window.open：站点是后端直出的独立页面（/site/<slug>/），
  // 一旦离开这个 SPA 就没有任何回来的入口——部分移动浏览器还会把 _blank 当同标签跳转，
  // 用户只能靠浏览器后退键才回得来。所以移动端在应用内套一层带返回栏的预览。
  const isMobile = useIsMobileViewport();
  const [previewSite, setPreviewSite] = useState<SiteItem | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    setSiteError('');
    try {
      const { items } = await listSites();
      setSites(items);
    } catch (e) {
      setSiteError(t('加载站点列表失败：') + (e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  // 站点由壳指向的后端托管，展示、复制、打开、预览用的是同一个绝对地址。相对路径
  // 在桌面端会解析成随机端口的本地反代，用户看到和复制走的都会变成只有本机可达的
  // 链接（站点本身在云端，公网地址是现成的）。
  const siteUrl = (site: SiteItem) => `${stablePublicOrigin()}${site.url}`;

  /** 打开站点：桌面另开标签页，手机走应用内预览（见 previewSite 的说明）。 */
  const openSite = (site: SiteItem) => {
    if (isMobile) {
      setPreviewSite(site);
      return;
    }
    window.open(siteUrl(site), '_blank', 'noopener,noreferrer');
  };

  const handleCopy = async (site: SiteItem) => {
    // 走统一的 copyToClipboard：测试机是 http://内网IP 访问，非安全上下文下
    // navigator.clipboard 根本不存在，直接调用必然落到「复制失败」——这正是
    // 「复制链接功能不可用」的原因。该工具在这种环境下退回 execCommand。
    if (await copyToClipboard(siteUrl(site))) {
      message.success(t('链接已复制'));
    } else {
      message.error(t('复制失败，请手动复制'));
    }
  };

  const handleDelete = async (site: SiteItem) => {
    try {
      await deleteSite(site.site_id, site.origin);
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

  const cardApps = applications.apps.filter((app) =>
    isMcpApplication(app) || !sites.some((site) => site.site_id === app.site_id),
  );
  const filteredApps = kw ? cardApps.filter((app) =>
    [app.title, app.id, ...app.tools.flatMap((tool) => [tool.name, tool.description])]
      .some((value) => value.toLowerCase().includes(kw)),
  ) : cardApps;
  const listLoading = loading || applications.loading;
  const empty = sites.length === 0 && cardApps.length === 0;
  const noMatches = filteredSites.length === 0 && filteredApps.length === 0;

  // ── My sites list / management view (the build entry is in the main chat; clicking "Create" jumps to a main-chat building session) ──
  return (
    <div className="jx-agentPage">
      <div className="jx-agentPage-header">
        <div>
          <div className="jx-agentPage-title">{t('站点')}</div>
          <div className="jx-agentPage-subtitle">{t('在同一处访问和管理你的站点与 MCP 服务')}</div>
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
          placeholder={t('搜索站点或 MCP 服务')}
        />

        {siteError && <Alert type="error" showIcon message={siteError}
          action={<Button onClick={() => void reload()}>{t('重试')}</Button>} />}
        {applications.error && <Alert type="error" showIcon message={applications.error}
          action={<Button onClick={() => void applications.reload()}>{t('重试')}</Button>} />}
        {listLoading && empty ? <Skeleton active paragraph={{ rows: 3 }} /> : !listLoading && empty ? (
          <Empty
            image={<AppstoreOutlined style={{ fontSize: 44, opacity: 0.35 }} />}
            description={<div className="jx-sites-emptyTitle">{t('暂无站点或 MCP 服务')}</div>}
            style={{ marginTop: 80 }}
          >
            <Button onClick={startSiteCreation}>{t('创建新站点')}</Button>
          </Empty>
        ) : !listLoading && noMatches ? (
          /* 搜不到时原来渲染的是一个空的列表容器——页面看着像卡住了。
             区分「一个站点都没有」和「有站点但没搜到」两种空态。 */
          <Empty
            image={<SearchOutlined style={{ fontSize: 44, opacity: 0.35 }} />}
            description={(
              <>
                <div className="jx-sites-emptyTitle">{t('没有匹配的站点或 MCP 服务')}</div>
                <div className="jx-sites-emptyDesc">{t('换个关键词试试')}</div>
              </>
            )}
            style={{ marginTop: 80 }}
          />
        ) : (
          <div className="jx-sites-list">
            {filteredSites.map((site) => (
              <HostedResourceCard key={site.site_id} title={site.title}
                tags={<><Tag>{t('站点')}</Tag><VisibilityTag site={site} /><SitePasswordTag site={site} /></>}
                address={<a
                    className="jx-sites-cardUrl"
                    href={siteUrl(site)}
                    target="_blank"
                    rel="noopener noreferrer"
                    onClick={(e) => {
                      if (!isMobile) return;
                      e.preventDefault();
                      setPreviewSite(site);
                    }}
                  >
                    {siteUrl(site)}
                  </a>}
                meta={<>
                    {t('版本')} v{site.current_version} · {site.file_count} {t('个文件')} ·{' '}
                    {formatSize(site.total_size_bytes)} · <EyeOutlined /> {site.view_count} {t('次访问')}
                    {site.updated_at ? ` · ${t('更新于')} ${formatDate(site.updated_at, '')}` : ''}
                </>}
                actions={<>
                  <Button
                    size="small"
                    type="primary"
                    ghost
                    onClick={() => openSite(site)}
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
                      title={site.project_id ? t('当前项目只读') : t('该站点没有源码工程，无法在线编辑')}
                    >
                      {t('编辑')}
                    </Button>
                  )}
                  {site.can_manage && <>
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
                  </>}
                </>}
              />
            ))}
            {filteredApps.map((app) => <ApplicationCard key={app.id} app={app}
              siteTitle={sites.find((site) => site.site_id === app.site_id)?.title}
              editing={editingApplication === app.id}
              onOpen={(view) => {
                if (view === 'edit' && isMcpApplication(app)) {
                  if (editingApplication) return;
                  setEditingApplication(app.id);
                  void startMcpEdit(app).finally(() => setEditingApplication(null));
                } else setActiveApplication({ app, view });
              }} />)}
          </div>
        )}
      </div>

      {activeApplication && <ApplicationManageModal
        key={`${activeApplication.app.id}:${activeApplication.view}`}
        app={activeApplication.app} view={activeApplication.view}
        onClose={() => setActiveApplication(null)}
        onChanged={() => void applications.reload()}
      />}

      {managing ? (
        <SiteManageModal
          site={managing}
          onClose={() => { setManaging(null); void applications.reload(); }}
          onChanged={(updated) => {
            setSites((prev) => prev.map((s) => (s.site_id === updated.site_id ? updated : s)));
            // 弹窗开着时也换成最新站点，省得里面的控件各自再存一份镜像状态。
            setManaging((prev) => (prev && prev.site_id === updated.site_id ? updated : prev));
          }}
        />
      ) : null}

      {/* 移动端站点预览：整屏 iframe + 顶部返回栏。站点本身是后端直出的独立页面，
          没法在它内部放返回入口，所以把返回入口留在这一层。 */}
      {previewSite ? (
        <div className="jx-sitePreview" role="dialog" aria-modal="true" aria-label={t('站点预览')}>
          <div className="jx-sitePreview-bar">
            <button
              type="button"
              className="jx-sitePreview-back"
              onClick={() => setPreviewSite(null)}
              aria-label={t('返回站点列表')}
            >
              <ArrowLeftOutlined />
            </button>
            <span className="jx-sitePreview-title">{previewSite.title}</span>
            <button
              type="button"
              className="jx-sitePreview-external"
              onClick={() => window.open(siteUrl(previewSite), '_blank', 'noopener,noreferrer')}
              aria-label={t('在新窗口打开')}
            >
              <ExportOutlined />
            </button>
          </div>
          <iframe
            className="jx-sitePreview-frame"
            src={siteUrl(previewSite)}
            title={previewSite.title}
          />
        </div>
      ) : null}
    </div>
  );
}
