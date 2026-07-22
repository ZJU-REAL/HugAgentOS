import { useEffect, useMemo, useRef, useState, type ReactElement } from 'react';
import {
  Modal, Switch, Button, Tag, List, Typography, Slider, message, Tabs, Empty, Input, Select,
  Skeleton, Spin, Tooltip,
} from 'antd';
import {
  ApiOutlined, AppstoreOutlined, CheckOutlined, CloseOutlined, DatabaseOutlined,
  DeploymentUnitOutlined, EditOutlined, ExclamationCircleFilled, FileTextOutlined,
  KeyOutlined, LinkOutlined, LockOutlined, LogoutOutlined, MessageOutlined, RobotOutlined,
  TeamOutlined, UserOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons';
import { AnimatePresence, motion } from 'motion/react';
import { useSettingsStore, useAuthStore, useCatalogStore, useUIStore, useEditionStore, usePluginStore } from '../../stores';
import { DUR, EASE, SLIDE_EASE } from '../../utils/motionTokens';
import type { MemoryItem } from '../../types';
import { resolveAvatarUrl } from '../../utils/avatar';
import { mdToHtml } from '../../utils/markdown';
import {
  getMyProfile,
  getMySystemAccess,
  getOntologyGovernanceAccess,
  setMyAvatarUrl,
  updateMyProfile,
  uploadMyAvatar,
} from '../../api';
import { TeamsSection } from './TeamsSection';
import { ApiKeyPanel } from './ApiKeyPanel';
import { ChannelBotsPanel } from './ChannelBotsPanel';
import { SystemModelPanel } from './SystemModelPanel';
import { SystemServicePanel } from './SystemServicePanel';
import { MyLogsPanel } from './MyLogsPanel';
import { PasswordManagementPanel } from './PasswordManagementPanel';
import { FactsList } from '../memory/FactsList';
import { OntologyManager } from '../ontology';
import { roleLabel } from '../../utils/roles';
import { getLang, setLang, t, type Lang } from '../../i18n';

interface SectionDef {
  id: string;
  label: string;
  icon: ReactElement;
}

const SETTINGS_SECTIONS: SectionDef[] = [
  { id: 'profile', label: t('个人信息'), icon: <UserOutlined /> },
  { id: 'session', label: t('会话设置'), icon: <MessageOutlined /> },
  { id: 'memory', label: t('记忆设置'), icon: <DatabaseOutlined /> },
  { id: 'ontology', label: t('本体校验'), icon: <SafetyCertificateOutlined /> },
  { id: 'enabled', label: t('已启用清单'), icon: <AppstoreOutlined /> },
  { id: 'teams', label: t('团队管理'), icon: <TeamOutlined /> },
];

const API_KEY_SECTION: SectionDef = { id: 'apikey', label: 'API-Key', icon: <KeyOutlined /> };
const PASSWORD_SECTION: SectionDef = { id: 'password', label: t('密码管理'), icon: <LockOutlined /> };
const CHANNELS_SECTION: SectionDef = { id: 'channels', label: t('我的机器人'), icon: <RobotOutlined /> };
// CE personal system settings (EE uses the /config system console to avoid dual entry points; see the /v1/me/system/access probe)
const SYS_MODEL_SECTION: SectionDef = { id: 'sysmodel', label: t('模型服务'), icon: <DeploymentUnitOutlined /> };
const SYS_SERVICE_SECTION: SectionDef = { id: 'sysservice', label: t('服务配置'), icon: <ApiOutlined /> };
const MY_LOGS_SECTION: SectionDef = { id: 'mylogs', label: t('我的日志'), icon: <FileTextOutlined /> };

const AVATAR_CROP_SIZE = 320;
const AVATAR_OUTPUT_SIZE = 256;
const MIN_ZOOM = 1;
const MAX_ZOOM = 3;

const DEFAULT_AVATARS = Array.from({ length: 8 }, (_, i) => ({
  id: `default-avatar-${i + 1}`,
  url: `/icons/avatar/avatar-${i + 1}.png`,
}));

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

function getCropBounds(imageWidth: number, imageHeight: number, zoom: number) {
  const baseScale = Math.max(AVATAR_CROP_SIZE / imageWidth, AVATAR_CROP_SIZE / imageHeight);
  const scale = baseScale * zoom;
  const displayWidth = imageWidth * scale;
  const displayHeight = imageHeight * scale;
  const maxOffsetX = Math.max(0, (displayWidth - AVATAR_CROP_SIZE) / 2);
  const maxOffsetY = Math.max(0, (displayHeight - AVATAR_CROP_SIZE) / 2);
  return {
    scale,
    displayWidth,
    displayHeight,
    maxOffsetX,
    maxOffsetY,
  };
}

export default function SettingsPage() {
  const { dispatchProcessVisible, setDispatchProcessVisible } = useUIStore();
  const {
    memoryEnabled,
    memoryWriteEnabled,
    memoryServiceAvailable,
    embeddingAvailable,
    memoryItems,
    memoryPanelOpen,
    memoryLoading,
    memoryProfile,
    memoryGraph,
    memoryGraphEnabled,
    lastToggleError,
    ontologyEnabled,
    ontologyAvailable,
    ontologyActivePacks,
    setMemoryPanelOpen,
    toggleMemory,
    toggleMemoryWrite,
    toggleOntology,
    loadOntologySettings,
    loadMemoryAllLayers,
    removeMemory,
    clearMemories,
  } = useSettingsStore();

  const { authUser, doLogout, setAvatarUrl, setAuthUser, loggingOut } = useAuthStore();
  const apiKeyEnabled = authUser?.can_use_api_key === true;
  const channelBotEnabled = authUser?.can_create_channel_bot === true;
  // Team management is a multi-tenancy capability bit (the whole section is hidden under CE / unlicensed license)
  const multiTenancy = useEditionStore((s) => (s.loaded ? !!s.features.multi_tenancy : true));
  // CE personal system settings: shown only in the community edition (EE uses the /config system console), and only when the backend probe permits it —
  // model service / service config; "My logs" is the user's own data, visible to all logged-in users under CE.
  const isCE = useEditionStore((s) => (s.loaded ? s.edition === 'ce' : false));
  const [sysAccess, setSysAccess] = useState(false);
  const [ontologyGovernanceAccess, setOntologyGovernanceAccess] = useState(false);
  const [logoutConfirmOpen, setLogoutConfirmOpen] = useState(false);
  useEffect(() => {
    if (!isCE) { setSysAccess(false); return; }
    getMySystemAccess()
      .then((info) => setSysAccess(!!info.allowed))
      .catch(() => setSysAccess(false));
  }, [isCE, authUser?.user_id]);
  useEffect(() => {
    if (!isCE) { setOntologyGovernanceAccess(false); return; }
    getOntologyGovernanceAccess()
      .then((info) => setOntologyGovernanceAccess(!!info.allowed))
      .catch(() => setOntologyGovernanceAccess(false));
  }, [isCE, authUser?.user_id]);
  // Only show a section in the settings center when the admin has enabled the corresponding capability bit
  const sections = useMemo<SectionDef[]>(() => {
    let base = multiTenancy ? SETTINGS_SECTIONS : SETTINGS_SECTIONS.filter((s) => s.id !== 'teams');
    if (isCE && ontologyGovernanceAccess) {
      base = base.map((section) => (
        section.id === 'ontology' ? { ...section, label: t('本体治理') } : section
      ));
    }
    if (isCE) base = [base[0], PASSWORD_SECTION, ...base.slice(1)];
    if (apiKeyEnabled) base = [...base, API_KEY_SECTION];
    if (channelBotEnabled) base = [...base, CHANNELS_SECTION];
    if (isCE && sysAccess) base = [...base, SYS_MODEL_SECTION, SYS_SERVICE_SECTION];
    if (isCE) base = [...base, MY_LOGS_SECTION];
    return base;
  }, [apiKeyEnabled, channelBotEnabled, multiTenancy, isCE, ontologyGovernanceAccess, sysAccess]);
  const [editingNickname, setEditingNickname] = useState(false);
  const [nicknameDraft, setNicknameDraft] = useState('');
  const [savingNickname, setSavingNickname] = useState(false);
  // External SSO account profiles are managed by the identity source; the backend PATCH /v1/me returns 403 —— the frontend just grays out the entry
  const [authSource, setAuthSource] = useState<'local' | 'external' | null>(null);
  useEffect(() => {
    if (!authUser?.user_id) return;
    getMyProfile()
      .then((p) => setAuthSource(p.auth_source ?? null))
      .catch(() => setAuthSource(null));
  }, [authUser?.user_id]);
  const isExternalAccount = authSource === 'external';

  const currentDisplayName = authUser?.nickname || authUser?.real_name || authUser?.username || t('未登录');

  const beginEditNickname = () => {
    setNicknameDraft(authUser?.nickname || authUser?.real_name || '');
    setEditingNickname(true);
  };
  const cancelEditNickname = () => {
    setEditingNickname(false);
    setNicknameDraft('');
  };
  const commitEditNickname = async () => {
    const next = nicknameDraft.trim();
    if (!next) {
      messageApi.error(t('用户名不能为空'));
      return;
    }
    if (next.length > 32) {
      messageApi.error(t('用户名长度不能超过 32 位'));
      return;
    }
    if (next === (authUser?.nickname || '')) {
      setEditingNickname(false);
      return;
    }
    setSavingNickname(true);
    try {
      const updated = await updateMyProfile({ nickname: next });
      if (authUser) {
        setAuthUser({
          ...authUser,
          nickname: updated.nickname ?? next,
          real_name: updated.real_name ?? authUser.real_name,
        });
      }
      messageApi.success(t('用户名已更新'));
      setEditingNickname(false);
    } catch (err) {
      messageApi.error((err as Error)?.message || t('更新失败'));
    } finally {
      setSavingNickname(false);
    }
  };
  const { catalog } = useCatalogStore();
  const installedPlugins = usePluginStore((s) => s.installed);
  // The enabled list needs to show plugins → fetch installed plugins once on mount (the store dedupes and skips if already loaded)
  useEffect(() => { void usePluginStore.getState().fetchInstalled(); }, []);
  const [messageApi, contextHolder] = message.useMessage();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const dragStartRef = useRef<{ x: number; y: number; offsetX: number; offsetY: number } | null>(null);
  const [activeSection, setActiveSection] = useState<string>('profile');
  const [passwordModalOpen, setPasswordModalOpen] = useState(false);
  const [memoryTab, setMemoryTab] = useState('profile');
  // Switch optimistic-update rollback on failure → shake the corresponding row once
  const [shakeRowKey, setShakeRowKey] = useState<'memory' | 'memoryWrite' | 'ontology' | null>(null);
  useEffect(() => {
    if (!lastToggleError) return;
    setShakeRowKey(lastToggleError.key);
    const timer = window.setTimeout(() => setShakeRowKey(null), 400);
    return () => window.clearTimeout(timer);
  }, [lastToggleError]);
  const [avatarPickerOpen, setAvatarPickerOpen] = useState(false);
  const [cropModalOpen, setCropModalOpen] = useState(false);
  const [selectedImageUrl, setSelectedImageUrl] = useState<string | null>(null);
  const [selectedImageName, setSelectedImageName] = useState('');
  const [imageNaturalSize, setImageNaturalSize] = useState<{ width: number; height: number } | null>(null);
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });

  // Group enabled items by kind for display.
  // Plugins are enabled by default and only count as disabled when explicitly enabled === false (consistent with the capability center); the rest follow the catalog's enabled bit.
  const enabledGroups = [
    { label: t('技能'), tags: catalog.skills.filter((i) => i.enabled).map((i) => ({ key: i.id, name: i.name })) },
    { label: t('智能体'), tags: catalog.agents.filter((i) => i.enabled).map((i) => ({ key: i.id, name: i.name })) },
    { label: t('系统工具'), tags: catalog.mcp.filter((i) => i.enabled).map((i) => ({ key: i.id, name: i.name })) },
    { label: t('插件'), tags: installedPlugins.filter((p) => p.enabled !== false).map((p) => ({ key: p.install_id, name: p.name })) },
  ];
  const hasEnabledItems = enabledGroups.some((g) => g.tags.length > 0);
  const avatarUrl = resolveAvatarUrl(authUser?.avatar_url);
  const defaultAvatars = DEFAULT_AVATARS;

  const cropBounds = useMemo(() => {
    if (!imageNaturalSize) return null;
    return getCropBounds(imageNaturalSize.width, imageNaturalSize.height, zoom);
  }, [imageNaturalSize, zoom]);

  useEffect(() => () => {
    if (selectedImageUrl?.startsWith('blob:')) {
      URL.revokeObjectURL(selectedImageUrl);
    }
  }, [selectedImageUrl]);

  useEffect(() => {
    if (!sections.some((section) => section.id === activeSection)) {
      setActiveSection(sections[0]?.id ?? 'profile');
    }
  }, [activeSection, sections]);

  const closeCropModal = () => {
    setCropModalOpen(false);
    setImageNaturalSize(null);
    setZoom(1);
    setOffset({ x: 0, y: 0 });
    if (selectedImageUrl?.startsWith('blob:')) {
      URL.revokeObjectURL(selectedImageUrl);
    }
    setSelectedImageUrl(null);
    setSelectedImageName('');
  };

  const handleAvatarFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    if (!file.type.startsWith('image/')) {
      messageApi.error(t('请上传图片文件'));
      return;
    }
    if (file.size > 8 * 1024 * 1024) {
      messageApi.error(t('图片请控制在 8MB 以内'));
      return;
    }

    if (selectedImageUrl?.startsWith('blob:')) {
      URL.revokeObjectURL(selectedImageUrl);
    }

    setSelectedImageUrl(URL.createObjectURL(file));
    setSelectedImageName(file.name);
    setImageNaturalSize(null);
    setZoom(1);
    setOffset({ x: 0, y: 0 });
    setAvatarPickerOpen(false);
    setCropModalOpen(true);
  };

  const [savingAvatar, setSavingAvatar] = useState(false);

  const handleUseDefaultAvatar = async (url: string) => {
    setSavingAvatar(true);
    try {
      const result = await setMyAvatarUrl(url);
      setAvatarUrl(result.avatar_url);
      setAvatarPickerOpen(false);
      messageApi.success(t('头像已更新'));
    } catch (err) {
      messageApi.error((err as Error)?.message || t('头像更新失败'));
    } finally {
      setSavingAvatar(false);
    }
  };

  const handleImageLoad = (event: React.SyntheticEvent<HTMLImageElement>) => {
    const { naturalWidth, naturalHeight } = event.currentTarget;
    setImageNaturalSize({ width: naturalWidth, height: naturalHeight });
    setOffset({ x: 0, y: 0 });
  };

  const updateOffset = (nextX: number, nextY: number) => {
    if (!cropBounds) {
      setOffset({ x: nextX, y: nextY });
      return;
    }
    setOffset({
      x: clamp(nextX, -cropBounds.maxOffsetX, cropBounds.maxOffsetX),
      y: clamp(nextY, -cropBounds.maxOffsetY, cropBounds.maxOffsetY),
    });
  };

  const handlePointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!cropBounds) return;
    dragStartRef.current = {
      x: event.clientX,
      y: event.clientY,
      offsetX: offset.x,
      offsetY: offset.y,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const handlePointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!dragStartRef.current) return;
    const nextX = dragStartRef.current.offsetX + (event.clientX - dragStartRef.current.x);
    const nextY = dragStartRef.current.offsetY + (event.clientY - dragStartRef.current.y);
    updateOffset(nextX, nextY);
  };

  const handlePointerUp = (event: React.PointerEvent<HTMLDivElement>) => {
    dragStartRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };

  const handleZoomChange = (value: number) => {
    const nextZoom = Array.isArray(value) ? value[0] : value;
    setZoom(nextZoom);
    if (!imageNaturalSize) return;
    const nextBounds = getCropBounds(imageNaturalSize.width, imageNaturalSize.height, nextZoom);
    setOffset((prev) => ({
      x: clamp(prev.x, -nextBounds.maxOffsetX, nextBounds.maxOffsetX),
      y: clamp(prev.y, -nextBounds.maxOffsetY, nextBounds.maxOffsetY),
    }));
  };

  const handleSaveAvatar = async () => {
    if (!selectedImageUrl || !imageNaturalSize || !cropBounds) return;
    const image = new Image();
    image.src = selectedImageUrl;
    await image.decode();

    const canvas = document.createElement('canvas');
    canvas.width = AVATAR_OUTPUT_SIZE;
    canvas.height = AVATAR_OUTPUT_SIZE;
    const context = canvas.getContext('2d');
    if (!context) {
      messageApi.error(t('头像处理失败，请重试'));
      return;
    }

    const left = (AVATAR_CROP_SIZE - cropBounds.displayWidth) / 2 + offset.x;
    const top = (AVATAR_CROP_SIZE - cropBounds.displayHeight) / 2 + offset.y;
    const sourceX = Math.max(0, (0 - left) / cropBounds.scale);
    const sourceY = Math.max(0, (0 - top) / cropBounds.scale);
    const sourceSize = AVATAR_CROP_SIZE / cropBounds.scale;

    context.imageSmoothingEnabled = true;
    context.drawImage(
      image,
      sourceX,
      sourceY,
      sourceSize,
      sourceSize,
      0,
      0,
      AVATAR_OUTPUT_SIZE,
      AVATAR_OUTPUT_SIZE,
    );

    const blob: Blob | null = await new Promise((resolve) =>
      canvas.toBlob((b) => resolve(b), 'image/png'),
    );
    if (!blob) {
      messageApi.error(t('头像处理失败，请重试'));
      return;
    }

    setSavingAvatar(true);
    try {
      const result = await uploadMyAvatar(blob, 'avatar.png');
      setAvatarUrl(result.avatar_url);
      messageApi.success(t('头像已更新'));
      closeCropModal();
    } catch (err) {
      messageApi.error((err as Error)?.message || t('头像上传失败'));
    } finally {
      setSavingAvatar(false);
    }
  };

  return (
    <>
      {contextHolder}
      <div className={`jx-settings-shell${isCE && ontologyGovernanceAccess && activeSection === 'ontology' ? ' jx-settings-shell--wide' : ''}`}>
        <nav className="jx-settings-nav" aria-label={t('系统设置')}>
          <p className="jx-settings-navTitle">{t('系统设置')}</p>
          {sections.map((s) => (
            <button
              key={s.id}
              type="button"
              className={`jx-settings-navItem${activeSection === s.id ? ' active' : ''}`}
              onClick={() => setActiveSection(s.id)}
            >
              {activeSection === s.id && (
                <motion.span
                  layoutId="settings-nav-indicator"
                  className="jx-settings-navIndicator"
                  transition={{ duration: 0.25, ease: SLIDE_EASE }}
                />
              )}
              <span className="jx-settings-navIcon">{s.icon}</span>
              {s.label}
            </button>
          ))}
        </nav>
      <div className="jx-settings-page">
        <div className="jx-settings-pageHeader">
          <h2 className="jx-settings-title">
            {sections.find((section) => section.id === activeSection)?.label ?? t('系统设置')}
          </h2>
          <p className="jx-settings-subtitle">{t('每次只显示一个设置模块，修改会立即保存或在提交后生效。')}</p>
        </div>

        <AnimatePresence mode="wait" initial={false}>
          <motion.div
            key={activeSection}
            className="jx-settings-module"
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: DUR.fast, ease: EASE.standard }}
          >

        {/* ── Personal info ─────────────────────────────────── */}
        {activeSection === 'profile' && (
        <section id="section-profile" className="jx-settings-section">
        <h3 className="jx-settings-section-title">{t('个人信息')}</h3>
        <div className="jx-settings-card jx-settings-card--padded">
          <div className="jx-settings-userInfo">
            <div className="jx-settings-avatarWrap">
              <button type="button" className="jx-settings-avatarHoverBtn" onClick={() => setAvatarPickerOpen(true)} title={t('更换头像')}>
                <AnimatePresence mode="popLayout" initial={false}>
                  <motion.img
                    key={avatarUrl}
                    src={avatarUrl}
                    alt=""
                    className="jx-settings-avatar"
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                    transition={{ duration: 0.25, ease: EASE.standard }}
                  />
                </AnimatePresence>
                <div className="jx-settings-avatarOverlay" aria-hidden="true">
                  <EditOutlined style={{ fontSize: 18, color: '#fff' }} />
                </div>
              </button>
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                style={{ display: 'none' }}
                onChange={handleAvatarFileChange}
              />
            </div>
            <div className="jx-settings-userMeta">
              <AnimatePresence mode="wait" initial={false}>
                {editingNickname ? (
                  <motion.div
                    key="nickname-edit"
                    initial={{ opacity: 0, y: -4 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -4 }}
                    transition={{ duration: 0.15, ease: EASE.standard }}
                    style={{ display: 'flex', alignItems: 'center', gap: 6 }}
                  >
                    <Input
                      size="small"
                      autoFocus
                      value={nicknameDraft}
                      onChange={(e) => setNicknameDraft(e.target.value)}
                      maxLength={32}
                      placeholder={t('请输入用户名')}
                      onPressEnter={() => void commitEditNickname()}
                      disabled={savingNickname}
                      style={{ width: 200 }}
                    />
                    <Button
                      type="text"
                      size="small"
                      icon={<CheckOutlined />}
                      loading={savingNickname}
                      onClick={() => void commitEditNickname()}
                    />
                    <Button
                      type="text"
                      size="small"
                      icon={<CloseOutlined />}
                      disabled={savingNickname}
                      onClick={cancelEditNickname}
                    />
                  </motion.div>
                ) : (
                  <motion.span
                    key="nickname-view"
                    initial={{ opacity: 0, y: 4 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: 4 }}
                    transition={{ duration: 0.15, ease: EASE.standard }}
                    className="jx-settings-userName"
                    style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}
                  >
                    {currentDisplayName}
                    {authUser && (
                      isExternalAccount ? (
                        <Tooltip title={t('外部登录账号的用户名由身份源管理，请到身份源修改')}>
                          <span style={{ display: 'inline-flex' }}>
                            <Button
                              type="text"
                              size="small"
                              icon={<EditOutlined />}
                              disabled
                            />
                          </span>
                        </Tooltip>
                      ) : (
                        <Button
                          type="text"
                          size="small"
                          icon={<EditOutlined />}
                          onClick={beginEditNickname}
                          title={t('修改用户名')}
                        />
                      )
                    )}
                  </motion.span>
                )}
              </AnimatePresence>
              {authUser?.username && (
                <span className="jx-settings-userId">
                  ID: {authUser.username}
                </span>
              )}
              {(authUser?.department || (authUser?.teams && authUser.teams.length > 0)) && (
                <div className="jx-settings-userTeams">
                  {authUser?.department && (
                    <Tag color="geekblue" icon={<LinkOutlined />} style={{ marginInlineEnd: 4 }}>
                      {authUser.department}
                    </Tag>
                  )}
                  {authUser?.teams?.map((t) => {
                    const isSso = t.source === 'sso_auto';
                    if (isSso && t.name === authUser?.department) return null;
                    return (
                      <Tag
                        key={t.team_id}
                        color={isSso ? 'cyan' : 'blue'}
                        icon={isSso ? <LinkOutlined /> : <TeamOutlined />}
                      >
                        {t.name} · {roleLabel(t.role)}
                      </Tag>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        </div>
        </section>
        )}

        {/* ── CE password management ─────────────────────────────── */}
        {isCE && activeSection === 'password' && (
          <section id="section-password" className="jx-settings-section">
            <h3 className="jx-settings-section-title">{t('密码管理')}</h3>
            <div className="jx-settings-card jx-settings-card--padded">
              <div className="jx-settings-securityAction">
                <div className="jx-settings-securityIcon" aria-hidden="true"><LockOutlined /></div>
                <div className="jx-settings-securityCopy">
                  <span className="jx-settings-rowLabel">{t('登录密码')}</span>
                  <span className="jx-settings-rowDesc">{t('使用独立弹窗修改密码，提交成功后立即生效。')}</span>
                </div>
                <Button type="primary" onClick={() => setPasswordModalOpen(true)}>{t('修改密码')}</Button>
              </div>
            </div>
          </section>
        )}

        {/* ── Session settings ─────────────────────────────────── */}
        {activeSection === 'session' && (
        <section id="section-session" className="jx-settings-section">
        <h3 className="jx-settings-section-title">{t('会话设置')}</h3>
        <div className="jx-settings-card">
          <div className="jx-settings-row">
            <div className="jx-settings-rowLeft">
              <span className="jx-settings-rowLabel">{t('显示调度过程')}</span>
              <span className="jx-settings-rowDesc">
                {t('控制对话中是否显示智能体的调度子智能体、MCP工具、技能等组件')}
              </span>
            </div>
            <Switch
              checked={dispatchProcessVisible}
              onChange={(checked) => setDispatchProcessVisible(checked)}
            />
          </div>

          <div className="jx-settings-divider" />

          <div className="jx-settings-row">
            <div className="jx-settings-rowLeft">
              <span className="jx-settings-rowLabel">{t('界面语言')}</span>
              <span className="jx-settings-rowDesc">
                {t('切换界面显示语言，切换后页面将自动刷新')}
              </span>
            </div>
            <Select
              value={getLang()}
              style={{ width: 140 }}
              options={[
                { value: 'zh-CN', label: t('简体中文') },
                { value: 'en', label: 'English' },
              ]}
              onChange={(v) => setLang(v as Lang)}
            />
          </div>
        </div>
        </section>
        )}

        {/* ── Memory settings ─────────────────────────────────── */}
        {activeSection === 'memory' && (
        <section id="section-memory" className="jx-settings-section">
        <h3 className="jx-settings-section-title">{t('记忆设置')}</h3>
        <div className="jx-settings-card">
          {/* Memory write */}
          <div className={`jx-settings-row${shakeRowKey === 'memoryWrite' ? ' jx-anim-shake' : ''}`}>
            <div className="jx-settings-rowLeft">
              <span className="jx-settings-rowLabel">{t('写入记忆')}</span>
              <span className="jx-settings-rowDesc">
                {t('开启后智能体会在每次对话结束后自动判断是否写入记忆')}
              </span>
            </div>
            <Switch
              checked={memoryWriteEnabled}
              disabled={!memoryServiceAvailable}
              onChange={(checked) => toggleMemoryWrite(checked)}
            />
          </div>

          <div className="jx-settings-divider" />

          {/* Permanent memory */}
          <div className={`jx-settings-row${shakeRowKey === 'memory' ? ' jx-anim-shake' : ''}`}>
            <div className="jx-settings-rowLeft">
              <span className="jx-settings-rowLabel">{t('永久记忆')}</span>
              <span className="jx-settings-rowDesc">
                {t('开启后 AI 将记住您跨会话的偏好和背景信息')}
              </span>
              {!memoryServiceAvailable && (
                <Tag>{t('当前实例未配置记忆服务')}</Tag>
              )}
              {memoryServiceAvailable && !embeddingAvailable && (
                <Tag color="warning">
                  {t('开启记忆前请先配置并分配 embedding 模型')}
                </Tag>
              )}
            </div>
            <Switch
              checked={memoryEnabled}
              disabled={!memoryServiceAvailable}
              onChange={(checked) => toggleMemory(checked)}
            />
          </div>

          <AnimatePresence initial={false}>
            {memoryEnabled && (
              <motion.div
                key="memory-detail"
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: 'auto', opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.22, ease: SLIDE_EASE }}
                style={{ overflow: 'hidden' }}
              >
                <div className="jx-settings-memoryDetail">
                  <span className="jx-settings-memoryCount">
                    {t('当前事实记忆 {n} 条', { n: memoryItems.length })}
                    {memoryProfile && memoryProfile.length > 0 ? ` · ${t('档案 {n} 字', { n: memoryProfile.length })}` : ''}
                  </span>
                  <a
                    className="jx-settings-memoryLink"
                    onClick={() => {
                      setMemoryPanelOpen(true);
                      void loadMemoryAllLayers();
                    }}
                  >
                    {t('查看分层详情')}
                  </a>
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
        </section>
        )}

        {/* ── Ontology validation ─────────────────────────────── */}
        {activeSection === 'ontology' && (
        <section id="section-ontology" className="jx-settings-section">
        <h3 className="jx-settings-section-title">{isCE && ontologyGovernanceAccess ? t('本体治理') : t('本体校验')}</h3>
        <div className="jx-settings-card">
          <div className={`jx-settings-row${shakeRowKey === 'ontology' ? ' jx-anim-shake' : ''}`}>
            <div className="jx-settings-rowLeft">
              <span className="jx-settings-rowLabel">{t('使用领域本体校验')}</span>
              <span className="jx-settings-rowDesc">
                {ontologyAvailable
                  ? t('开启后，工具调用会先经过领域规则门禁，高风险答案会在交付前由独立评审委员会校验')
                  : isCE && ontologyGovernanceAccess
                    ? t('请先在下方导入并发布可用的领域本体包')
                    : t('管理员尚未激活可用的领域本体包')}
              </span>
            </div>
            <Tooltip title={!ontologyAvailable ? t('暂无可用本体包') : undefined}>
              <span>
                <Switch
                  checked={ontologyEnabled && ontologyAvailable}
                  disabled={!ontologyAvailable}
                  onChange={(checked) => toggleOntology(checked)}
                />
              </span>
            </Tooltip>
          </div>
          {ontologyActivePacks.length > 0 && (
            <>
              <div className="jx-settings-divider" />
              <div className="jx-settings-row">
                <div className="jx-settings-rowLeft">
                  <span className="jx-settings-rowLabel">{t('当前领域包')}</span>
                  <span className="jx-settings-rowDesc">{t('仅在任务命中领域工作流时注入相关概念和规则')}</span>
                </div>
                <div className="jx-settings-tagWrap">
                  {ontologyActivePacks.map((pack) => (
                    <Tag key={pack.version_id} color="geekblue">
                      {pack.pack_id} · v{pack.version}
                    </Tag>
                  ))}
                </div>
              </div>
            </>
          )}
        </div>
        {isCE && ontologyGovernanceAccess && (
          <div className="jx-settings-card jx-settings-card--padded jx-settings-ontologyGovernance">
            <OntologyManager
              apiPrefix="/v1/ontologies/governance"
              onChanged={loadOntologySettings}
            />
          </div>
        )}
        </section>
        )}

        {/* ── Enabled list ────────────────────────────────── */}
        {activeSection === 'enabled' && (
        <section id="section-enabled" className="jx-settings-section">
        <h3 className="jx-settings-section-title">{t('已启用清单')}</h3>
        <div className="jx-settings-card">
          {enabledGroups.map((group) => group.tags.length > 0 && (
            <div key={group.label} className="jx-settings-enabledGroup">
              <span className="jx-settings-enabledLabel">{group.label}</span>
              <div className="jx-settings-tagWrap">
                {group.tags.map((tag) => (
                  <Tag key={tag.key} className="jx-settings-tag">{tag.name}</Tag>
                ))}
              </div>
            </div>
          ))}

          {!hasEnabledItems && (
            <div className="jx-settings-emptyHint">{t('当前未启用任何项')}</div>
          )}
        </div>
        </section>
        )}

        {/* ── Team management (multi-tenancy capability bit; hidden under CE / unlicensed) ──── */}
        {multiTenancy && activeSection === 'teams' && (
          <section id="section-teams" className="jx-settings-section">
          <h3 className="jx-settings-section-title">{t('团队管理')}</h3>
          <div className="jx-settings-card jx-settings-card--padded">
            <TeamsSection />
          </div>
          </section>
        )}

        {/* ── API-Key (shown only when the admin has enabled it) ─────────────────── */}
        {apiKeyEnabled && activeSection === 'apikey' && (
          <section id="section-apikey" className="jx-settings-section">
            <h3 className="jx-settings-section-title">API-Key</h3>
            <div className="jx-settings-card jx-settings-card--padded">
              <ApiKeyPanel />
            </div>
          </section>
        )}

        {/* ── My bots (shown only when the admin has enabled channel bots) ──────── */}
        {channelBotEnabled && activeSection === 'channels' && (
          <section id="section-channels" className="jx-settings-section">
            <h3 className="jx-settings-section-title">{t('我的机器人')}</h3>
            <div className="jx-settings-card jx-settings-card--padded">
              <ChannelBotsPanel />
            </div>
          </section>
        )}

        {/* ── CE personal system settings: model service / service config (shown only when the probe permits) ──── */}
        {isCE && sysAccess && activeSection === 'sysmodel' && (
          <section id="section-sysmodel" className="jx-settings-section">
            <h3 className="jx-settings-section-title">{t('模型服务')}</h3>
            <div className="jx-settings-card jx-settings-card--padded">
              <SystemModelPanel />
            </div>
          </section>
        )}

        {isCE && sysAccess && activeSection === 'sysservice' && (
          <section id="section-sysservice" className="jx-settings-section">
            <h3 className="jx-settings-section-title">{t('服务配置')}</h3>
            <div className="jx-settings-card jx-settings-card--padded">
              <SystemServicePanel />
            </div>
          </section>
        )}

        {/* ── My logs (CE: the user's own call logs and usage) ──────────────── */}
        {isCE && activeSection === 'mylogs' && (
          <section id="section-mylogs" className="jx-settings-section">
            <h3 className="jx-settings-section-title">{t('我的日志')}</h3>
            <div className="jx-settings-card jx-settings-card--padded">
              <MyLogsPanel />
            </div>
          </section>
        )}

        {/* ── Log out ──────────────────────────────────── */}
        {activeSection === 'profile' && <Button
          className="jx-settings-logoutBtn"
          icon={<LogoutOutlined />}
          onClick={() => setLogoutConfirmOpen(true)}
          block
        >
          {t('退出登录')}
        </Button>}
          </motion.div>
        </AnimatePresence>
      </div>
      </div>

      <Modal
        title={<span><ExclamationCircleFilled style={{ color: '#F8AB42', marginRight: 8 }} />{t('确认退出登录？')}</span>}
        open={logoutConfirmOpen}
        okText={t('退出登录')}
        cancelText={t('取消')}
        okButtonProps={{ danger: true }}
        confirmLoading={loggingOut}
        maskClosable={!loggingOut}
        closable={!loggingOut}
        onCancel={() => setLogoutConfirmOpen(false)}
        onOk={() => void doLogout()}
      >
        {t('退出登录不会丢失任何数据，你仍可以登录此账号。')}
      </Modal>

      <Modal
        title={t('修改密码')}
        open={passwordModalOpen}
        onCancel={() => setPasswordModalOpen(false)}
        footer={null}
        destroyOnHidden
        width={480}
        className="jx-passwordModal"
      >
        <PasswordManagementPanel onChanged={() => setPasswordModalOpen(false)} />
      </Modal>

      {/* Logout full-screen overlay: runs in parallel with the hard redirect, covering the intermediate frames after clearForLogout */}
      {loggingOut && (
        <div className="jx-settings-logoutMask">
          <Spin size="large" />
          <span>{t('正在退出…')}</span>
        </div>
      )}

      {/* Memory view modal */}
      <Modal
        title={t('选择头像')}
        open={avatarPickerOpen}
        onCancel={() => savingAvatar ? undefined : setAvatarPickerOpen(false)}
        footer={null}
        destroyOnHidden
        width={560}
      >
        <div className="jx-settings-avatarPicker">
          <div className="jx-settings-avatarPickerHead">
            <div>
              <div className="jx-settings-avatarPickerTitle">{t('默认头像')}</div>
              <div className="jx-settings-avatarPickerDesc">{t('你可以先从 8 个默认头像中选择，也可以继续从本地上传图片。')}</div>
            </div>
            <Button disabled={savingAvatar} onClick={() => fileInputRef.current?.click()}>{t('从本地上传')}</Button>
          </div>
          <div className="jx-settings-avatarGrid">
            {defaultAvatars.map((item, i) => (
              <motion.button
                key={item.id}
                type="button"
                disabled={savingAvatar}
                className={`jx-settings-avatarOption${avatarUrl === item.url ? ' active' : ''}`}
                onClick={() => void handleUseDefaultAvatar(item.url)}
                initial={{ opacity: 0, scale: 0.92 }}
                animate={{ opacity: 1, scale: 1 }}
                whileHover={{ y: -1, transition: { duration: DUR.instant, delay: 0 } }}
                transition={{ duration: DUR.fast, ease: EASE.brandOut, delay: 0.1 + i * 0.025 }}
              >
                <img src={item.url} alt={t('默认头像')} className="jx-settings-avatarOptionImage" />
              </motion.button>
            ))}
          </div>
        </div>
      </Modal>

      <Modal
        title={t('裁剪头像')}
        open={cropModalOpen}
        onCancel={closeCropModal}
        onOk={() => void handleSaveAvatar()}
        okText={t('保存头像')}
        cancelText={t('取消')}
        confirmLoading={savingAvatar}
        cancelButtonProps={{ disabled: savingAvatar }}
        maskClosable={!savingAvatar}
        destroyOnHidden
        width={520}
      >
        <div className="jx-settings-avatarCrop">
          <div
            className="jx-settings-avatarCropStage"
            onPointerDown={handlePointerDown}
            onPointerMove={handlePointerMove}
            onPointerUp={handlePointerUp}
            onPointerCancel={handlePointerUp}
          >
            {selectedImageUrl ? (
              <img
                src={selectedImageUrl}
                alt={selectedImageName}
                className="jx-settings-avatarCropImage"
                onLoad={handleImageLoad}
                style={cropBounds ? {
                  width: `${cropBounds.displayWidth}px`,
                  height: `${cropBounds.displayHeight}px`,
                  transform: `translate(${offset.x}px, ${offset.y}px)`,
                } : undefined}
              />
            ) : null}
            <div className="jx-settings-avatarCropMask" aria-hidden="true" />
          </div>
          <div className="jx-settings-avatarCropHint">
            {t('拖动图片调整位置，使用滑块缩放，保存后会同步更新头像。')}
          </div>
          <div className="jx-settings-avatarZoomRow">
            <span className="jx-settings-avatarZoomLabel">{t('缩放')}</span>
            <Slider min={MIN_ZOOM} max={MAX_ZOOM} step={0.01} value={zoom} onChange={handleZoomChange} />
          </div>
        </div>
      </Modal>

      <Modal
        title={t('我的分层记忆')}
        open={memoryPanelOpen}
        onCancel={() => setMemoryPanelOpen(false)}
        footer={null}
        width={720}
      >
        {memoryLoading ? (
          <Skeleton active title={false} paragraph={{ rows: 4 }} style={{ padding: '16px 0' }} />
        ) : (
          <Tabs
            activeKey={memoryTab}
            onChange={setMemoryTab}
            animated={{ tabPane: false }}
            destroyOnHidden
            items={[
              {
                key: 'profile',
                label: tabLabel(t('档案 L1'), memoryProfile?.length, t('字')),
                pane: renderProfileTab(memoryProfile),
              },
              {
                key: 'facts',
                label: tabLabel(t('事实 L2'), memoryItems.length),
                pane: renderFactsTab(memoryItems, removeMemory, clearMemories),
              },
              {
                key: 'graph',
                label: tabLabel(t('图谱 L3'), memoryGraphEnabled ? memoryGraph.length : undefined),
                pane: renderGraphTab(memoryGraph, memoryGraphEnabled),
              },
            ].map(({ key, label, pane }) => ({
              key,
              label,
              // destroyOnHidden remounts on Tab switch → uniformly play the enter animation once
              children: (
                <motion.div
                  key={`memory-tab-${key}`}
                  initial={{ opacity: 0, x: 8 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ duration: 0.18, ease: EASE.standard }}
                >
                  {pane}
                </motion.div>
              ),
            }))}
          />
        )}
      </Modal>
    </>
  );
}


// ─── Rendering of each layered-memory Tab ────────────────────────────────────────────────

function tabLabel(base: string, count: number | undefined, unit: string = ''): string {
  if (count === undefined || count === null || count === 0) return base;
  return `${base} · ${count}${unit}`;
}

function renderProfileTab(profile: import('../../types').MemoryProfile | null) {
  if (!profile || !profile.enabled) {
    return <Empty className="jx-anim-fadeIn" description={t('永久记忆未启用')} />;
  }
  const ratio = profile.max_chars > 0 ? Math.min(100, Math.round((profile.length / profile.max_chars) * 100)) : 0;
  if (!profile.content_md) {
    return (
      <div>
        <div style={{ fontSize: 12, color: '#888', marginBottom: 8 }}>
          {t('会话启动时冻结注入的用户画像 markdown（上限 {n} 字符）', { n: profile.max_chars })}
        </div>
        <Empty className="jx-anim-fadeIn" description={t('档案为空，开始对话后智能体会自动沉淀你的身份和偏好')} />
      </div>
    );
  }
  return (
    <div>
      <div style={{ fontSize: 12, color: '#888', marginBottom: 8 }}>
        {t('每次会话启动时冻结注入 · {used}/{max} 字 ({pct}%)', { used: profile.length, max: profile.max_chars, pct: ratio })}
      </div>
      <div className="jx-settings-profileMeter" role="progressbar" aria-valuenow={ratio} aria-valuemin={0} aria-valuemax={100}>
        <motion.div
          className={`jx-settings-profileMeterFill${ratio > 85 ? ' warn' : ''}`}
          initial={{ width: 0 }}
          animate={{ width: `${ratio}%` }}
          transition={{ duration: 0.5, ease: 'easeOut' }}
        />
      </div>
      <div
        style={{
          padding: 12, background: '#FAFAFA', borderRadius: 6,
          maxHeight: 400, overflow: 'auto', fontSize: 13, lineHeight: 1.7,
        }}
        dangerouslySetInnerHTML={{ __html: mdToHtml(profile.content_md) }}
      />
    </div>
  );
}

function renderFactsTab(
  items: MemoryItem[],
  removeMemory: (id: string) => Promise<void>,
  clearMemories: () => Promise<void>,
) {
  return <FactsList items={items} onRemove={removeMemory} onClearAll={clearMemories} />;
}

function renderGraphTab(
  relations: import('../../types').MemoryGraphRelation[],
  enabled: boolean,
) {
  if (!enabled) {
    return <Empty className="jx-anim-fadeIn" description={t('图谱记忆未启用（需配置 MEM0_GRAPH_ENABLED + Neo4j）')} />;
  }
  if (!relations.length) {
    return <Empty className="jx-anim-fadeIn" description={t('暂无实体关系')} />;
  }
  return (
    <List
      dataSource={relations}
      size="small"
      renderItem={(r, i) => (
        <List.Item>
          <motion.div
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.2, ease: EASE.brandOut, delay: Math.min(i, 10) * 0.03 }}
          >
            <Typography.Text style={{ fontSize: 13 }}>
              <Tag color="blue">{r.source}</Tag>
              <span style={{ color: '#888', margin: '0 8px' }}>{r.relationship}</span>
              <Tag color="green">{r.target}</Tag>
            </Typography.Text>
          </motion.div>
        </List.Item>
      )}
    />
  );
}
