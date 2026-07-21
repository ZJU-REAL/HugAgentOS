import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { motion } from 'motion/react';
import { Switch, Tag, Input, Typography, Button, Popconfirm, message, Dropdown, Modal, Form, Select, Pagination, Tooltip } from 'antd';
import { t } from '../../i18n';
import { SearchOutlined, LeftOutlined, PlusOutlined, DeleteOutlined, UploadOutlined, EditOutlined, DownOutlined, AppstoreAddOutlined, CloudUploadOutlined, DownloadOutlined, FileTextOutlined, SaveOutlined } from '@ant-design/icons';
import { useAgentStore, useCatalogStore, useAuthStore } from '../../stores';
import type { PanelKey, MarketplaceFetchers, MarketplaceSubmission, OntologyTagOption } from '../../types';
import { isCatalogKind, MARKETPLACE_CATEGORIES } from '../../utils/constants';
import { mdToHtml } from '../../utils/markdown';
import { staggerStyle } from '../../utils/motionTokens';
import { DRILL_IN_BACK, DRILL_IN_DETAIL } from '../../utils/motionVariants';
import { usePanelHeader } from '../../hooks/usePageConfig';
import { createMySkill, deleteMySkill, uploadMySkill, getMySkill, getMySkillFile, saveMySkillFile, deleteMySkillFile, uploadMySkillFile, exportMySkillZip, getMarketplaceSkills, getMarketplaceSkillDetail, installMarketplaceSkill, submitSkillToMarketplace, getMySkillSubmissions, withdrawSkillSubmission, getOntologyTagOptions, type MySkillFileInfo } from '../../api';
import { SkillMarketplaceModal } from './SkillMarketplaceModal';
import { SkillAvatar } from './skillIcons';
import { SkillIconPicker } from './SkillIconPicker';
import { OntologyBuildValidationModal } from '../common/OntologyBuildValidationModal';
import { OntologyTagSelect } from '../common/OntologyTagSelect';
import { getOntologyBuildFailure, type OntologyBuildFailure } from '../../utils/apiError';

const SKILLS_DETAIL_ID_STORAGE_KEY = 'hugagent_skills_detail_id';
const SKILLS_DETAIL_KIND_STORAGE_KEY = 'hugagent_skills_detail_kind';

// Cards per page in the grid (2-column layout, 6 rows)
const SKILLS_PAGE_SIZE = 12;

function loadSkillsDetailState(): { id: string | null; kind: 'skills' | 'agents' } {
  if (typeof window === 'undefined') {
    return { id: null, kind: 'skills' };
  }
  const id = window.localStorage.getItem(SKILLS_DETAIL_ID_STORAGE_KEY);
  const rawKind = window.localStorage.getItem(SKILLS_DETAIL_KIND_STORAGE_KEY);
  return {
    id: id || null,
    kind: rawKind === 'agents' ? 'agents' : 'skills',
  };
}

function saveSkillsDetailState(id: string | null, kind: 'skills' | 'agents') {
  if (typeof window === 'undefined') return;
  if (!id) {
    window.localStorage.removeItem(SKILLS_DETAIL_ID_STORAGE_KEY);
    window.localStorage.removeItem(SKILLS_DETAIL_KIND_STORAGE_KEY);
    return;
  }
  window.localStorage.setItem(SKILLS_DETAIL_ID_STORAGE_KEY, id);
  window.localStorage.setItem(SKILLS_DETAIL_KIND_STORAGE_KEY, kind);
}

export function SkillsPage({ embedded = false, onDetailChange }: { embedded?: boolean; onDetailChange?: (hasDetail: boolean) => void }) {
  const {
    catalog,
    panel,
    panelEntryNonce,
    manageQuery, setManageQuery,
    toggleItem,
  } = useCatalogStore();
  const { title: skillsTitle, subtitle: skillsSubtitle } = usePanelHeader('skills', {
    title: '技能库',
    subtitle: '启用/停用技能，并查看详细介绍、输入输出与示例',
  });

  const fetchCatalog = useCatalogStore((s) => s.fetchCatalog);
  const canAddSkill = useAuthStore((s) => s.authUser?.can_add_skill === true);
  const availableResources = useAgentStore((s) => s.availableResources);
  const fetchAvailableResources = useAgentStore((s) => s.fetchAvailableResources);

  const initialDetailState = embedded ? { id: null, kind: 'skills' as const } : loadSkillsDetailState();
  const [selectedId, setSelectedId] = useState<string | null>(initialDetailState.id);
  const [selectedKind, setSelectedKind] = useState<'skills' | 'agents'>(initialDetailState.kind);
  const [searchVisible, setSearchVisible] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [skillsPage, setSkillsPage] = useState(1);
  const [agentsPage, setAgentsPage] = useState(1);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  // Distinguish "user clicked navigation" from "localStorage restore / panel reset": only the former plays the list↔detail transition
  const [navDir, setNavDir] = useState<'detail' | 'list' | null>(null);

  const handleUploadSkill = useCallback(async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    if (!file.name.endsWith('.zip')) {
      message.error(t('请上传 .zip 技能包'));
      return;
    }
    setUploading(true);
    try {
      await uploadMySkill(file);
      message.success(t('技能已上传'));
      await fetchCatalog();
    } catch (err) {
      message.error((err as Error).message || t('上传失败'));
    } finally {
      setUploading(false);
    }
  }, [fetchCatalog]);

  const handleDeleteSkill = useCallback(async (id: string) => {
    try {
      await deleteMySkill(id);
      message.success(t('已删除'));
      await fetchCatalog();
    } catch (err) {
      message.error((err as Error).message || t('删除失败'));
    }
  }, [fetchCatalog]);

  const [marketplaceOpen, setMarketplaceOpen] = useState(false);
  const marketplaceFetchers = useMemo<MarketplaceFetchers>(() => ({
    loadList: () => getMarketplaceSkills(),
    loadDetail: (slug) => getMarketplaceSkillDetail(slug),
    install: (slug, secrets) => installMarketplaceSkill(slug, secrets),
  }), []);

  // ── Apply to list a skill on the marketplace (only your own private skills) ──────────────────────────────
  const [mySubs, setMySubs] = useState<MarketplaceSubmission[]>([]);
  const [applySkillId, setApplySkillId] = useState<string | null>(null);
  const [applying, setApplying] = useState(false);
  const [applyForm] = Form.useForm();

  const reloadSubmissions = useCallback(async () => {
    try {
      setMySubs(await getMySkillSubmissions());
    } catch {
      // List load failure does not bother the user, only affects the status marker
    }
  }, []);

  useEffect(() => {
    if (canAddSkill) void reloadSubmissions();
  }, [canAddSkill, reloadSubmissions]);

  // Take the most recent application per skill (the API returns newest→oldest)
  const subBySkill = useMemo(() => {
    const map = new Map<string, MarketplaceSubmission>();
    for (const s of mySubs) {
      if (!map.has(s.skill_id)) map.set(s.skill_id, s);
    }
    return map;
  }, [mySubs]);

  const openApply = useCallback((skillId: string) => {
    applyForm.resetFields();
    setApplySkillId(skillId);
  }, [applyForm]);

  const handleApply = useCallback(async () => {
    if (!applySkillId) return;
    const v = await applyForm.validateFields();
    setApplying(true);
    try {
      await submitSkillToMarketplace({
        skill_id: applySkillId,
        note: (v.note || '').trim(),
        category: (v.category || '').trim(),
        summary: (v.summary || '').trim(),
      });
      message.success(t('上架申请已提交，等待管理员审核'));
      setApplySkillId(null);
      await reloadSubmissions();
    } catch (e) {
      message.error((e as Error).message || t('提交失败'));
    } finally {
      setApplying(false);
    }
  }, [applySkillId, applyForm, reloadSubmissions]);

  const handleWithdraw = useCallback(async (submissionId: string) => {
    try {
      await withdrawSkillSubmission(submissionId);
      message.success(t('申请已撤回'));
      await reloadSubmissions();
    } catch (e) {
      message.error((e as Error).message || t('撤回失败'));
    }
  }, [reloadSubmissions]);

  const [handwriteOpen, setHandwriteOpen] = useState(false);
  const [creatingSkill, setCreatingSkill] = useState(false);
  // null = create mode; non-null = the skill id being edited (the skill id cannot be changed, disabled in the form)
  const [editingSkillId, setEditingSkillId] = useState<string | null>(null);
  const [loadingSkill, setLoadingSkill] = useState(false);
  const [skillIcon, setSkillIcon] = useState('');
  const [skillForm] = Form.useForm();
  const [ontologyTagOptions, setOntologyTagOptions] = useState<OntologyTagOption[]>([]);
  const [ontologyTagsLoading, setOntologyTagsLoading] = useState(false);
  const [buildFailure, setBuildFailure] = useState<OntologyBuildFailure | null>(null);

  useEffect(() => {
    if (!canAddSkill) return;
    void fetchAvailableResources();
    let cancelled = false;
    setOntologyTagsLoading(true);
    void getOntologyTagOptions('skill')
      .then((items) => { if (!cancelled) setOntologyTagOptions(items); })
      .catch(() => { if (!cancelled) setOntologyTagOptions([]); })
      .finally(() => { if (!cancelled) setOntologyTagsLoading(false); });
    return () => { cancelled = true; };
  }, [canAddSkill, fetchAvailableResources]);

  const skillMcpOptions = useMemo(
    () => (availableResources?.mcp_servers || []).map((server) => ({
      value: server.id,
      label: server.enabled ? server.name : `${server.name}${t('（未启用）')}`,
      description: server.description,
    })),
    [availableResources?.mcp_servers],
  );

  // ── Skill file management (edit mode) ────────────────────────────────────────────
  const [skillFiles, setSkillFiles] = useState<MySkillFileInfo[]>([]);
  const [editingFileName, setEditingFileName] = useState<string | null>(null);
  const [editingFileContent, setEditingFileContent] = useState('');
  const [fileLoading, setFileLoading] = useState(false);
  const [fileSaving, setFileSaving] = useState(false);
  const [newSkillFileName, setNewSkillFileName] = useState('');
  const skillFileInputRef = useRef<HTMLInputElement | null>(null);

  const resetSkillFileState = useCallback(() => {
    setSkillFiles([]);
    setEditingFileName(null);
    setEditingFileContent('');
    setNewSkillFileName('');
  }, []);

  const openCreateSkill = useCallback(() => {
    setEditingSkillId(null);
    setSkillIcon('');
    skillForm.resetFields();
    skillForm.setFieldsValue({ tags: [], ontology_tags: [], mcp_server_ids: [] });
    resetSkillFileState();
    setHandwriteOpen(true);
  }, [skillForm, resetSkillFileState]);

  const handleEditSkill = useCallback(async (id: string) => {
    setEditingSkillId(id);
    setHandwriteOpen(true);
    setLoadingSkill(true);
    skillForm.resetFields();
    resetSkillFileState();
    try {
      const detail = await getMySkill(id);
      setSkillIcon(detail.icon || '');
      skillForm.setFieldsValue({
        name: detail.id,
        display_name: detail.display_name,
        description: detail.description,
        tags: (detail.tags || []).filter((tag) => !tag.startsWith('ontology:')),
        ontology_tags: (detail.tags || []).filter((tag) => tag.startsWith('ontology:')),
        mcp_server_ids: detail.mcp_server_ids || [],
        instructions: detail.instructions,
      });
      setSkillFiles(detail.extra_files || []);
    } catch (e) {
      message.error((e as Error).message || t('加载技能失败'));
      setHandwriteOpen(false);
    } finally {
      setLoadingSkill(false);
    }
  }, [skillForm, resetSkillFileState]);

  const refreshSkillFiles = useCallback(async (id: string) => {
    try {
      const detail = await getMySkill(id);
      setSkillFiles(detail.extra_files || []);
    } catch {
      // Keep the current list when refresh fails
    }
  }, []);

  const openSkillFile = useCallback(async (filename: string, isBinary?: boolean) => {
    if (!editingSkillId) return;
    if (isBinary) {
      message.info(t('二进制文件不支持在线编辑，可删除后重新上传'));
      return;
    }
    setEditingFileName(filename);
    setFileLoading(true);
    try {
      const res = await getMySkillFile(editingSkillId, filename);
      if (res.is_binary) {
        message.info(t('二进制文件不支持在线编辑，可删除后重新上传'));
        setEditingFileName(null);
        return;
      }
      setEditingFileContent(res.content);
    } catch (e) {
      message.error((e as Error).message || t('加载文件失败'));
      setEditingFileName(null);
    } finally {
      setFileLoading(false);
    }
  }, [editingSkillId]);

  const handleSaveSkillFile = useCallback(async () => {
    if (!editingSkillId || !editingFileName) return;
    setFileSaving(true);
    try {
      await saveMySkillFile(editingSkillId, editingFileName, editingFileContent);
      message.success(t('文件已保存'));
      await refreshSkillFiles(editingSkillId);
      setEditingFileName(null);
      setEditingFileContent('');
    } catch (e) {
      message.error((e as Error).message || t('保存失败'));
    } finally {
      setFileSaving(false);
    }
  }, [editingSkillId, editingFileName, editingFileContent, refreshSkillFiles]);

  const handleDeleteSkillFile = useCallback(async (filename: string) => {
    if (!editingSkillId) return;
    try {
      await deleteMySkillFile(editingSkillId, filename);
      message.success(t('文件已删除'));
      setSkillFiles(prev => prev.filter(f => f.filename !== filename));
      if (editingFileName === filename) {
        setEditingFileName(null);
        setEditingFileContent('');
      }
    } catch (e) {
      message.error((e as Error).message || t('删除失败'));
    }
  }, [editingSkillId, editingFileName]);

  const handleUploadSkillFile = useCallback(async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files ? Array.from(e.target.files) : [];
    e.target.value = '';
    if (!editingSkillId || files.length === 0) return;
    for (const file of files) {
      try {
        await uploadMySkillFile(editingSkillId, file);
        message.success(t('已添加文件：{name}', { name: file.name }));
      } catch (err) {
        message.error((err as Error).message || t('上传失败'));
      }
    }
    await refreshSkillFiles(editingSkillId);
  }, [editingSkillId, refreshSkillFiles]);

  // Create an empty file: enter the editor first, only persist to the DB on "Save file"
  const handleCreateSkillFile = useCallback(() => {
    const name = newSkillFileName.trim();
    if (!name) { message.warning(t('请填写文件名')); return; }
    if (name === 'SKILL.md') { message.warning(t('SKILL.md 请在上方表单中编辑')); return; }
    if (skillFiles.some(f => f.filename === name)) {
      message.warning(t('已存在同名文件'));
      return;
    }
    setEditingFileName(name);
    setEditingFileContent('');
    setNewSkillFileName('');
  }, [newSkillFileName, skillFiles]);

  const handleExportSkill = useCallback(async (id: string) => {
    try {
      await exportMySkillZip(id);
      message.success(t('技能已导出'));
    } catch (e) {
      message.error((e as Error).message || t('导出失败'));
    }
  }, []);

  const handleCreateSkill = useCallback(async () => {
    const v = await skillForm.validateFields();
    setCreatingSkill(true);
    try {
      await createMySkill({
        name: v.name,
        display_name: v.display_name,
        description: (v.description || '').trim(),
        instructions: v.instructions,
        tags: Array.from(new Set([
          ...(Array.isArray(v.tags) ? v.tags.filter((tag: string) => !tag.startsWith('ontology:')) : []),
          ...(Array.isArray(v.ontology_tags) ? v.ontology_tags : []),
        ])),
        mcp_server_ids: Array.isArray(v.mcp_server_ids) ? v.mcp_server_ids : [],
        icon: skillIcon,
      });
      message.success(editingSkillId ? t('技能已更新') : t('技能已创建'));
      setHandwriteOpen(false);
      setEditingSkillId(null);
      skillForm.resetFields();
      resetSkillFileState();
      await fetchCatalog();
    } catch (e: unknown) {
      if (e && typeof e === 'object' && 'errorFields' in e) return;
      const ontologyFailure = getOntologyBuildFailure(e);
      if (ontologyFailure) {
        setBuildFailure(ontologyFailure);
      } else {
        message.error(e instanceof Error && e.message
          ? e.message
          : (editingSkillId ? t('更新失败') : t('创建失败')));
      }
    } finally {
      setCreatingSkill(false);
    }
  }, [skillForm, fetchCatalog, editingSkillId, skillIcon, resetSkillFileState]);

  const query = manageQuery.trim().toLowerCase();

  const filteredSkills = useMemo(() => {
    const arr = catalog.skills;
    return query ? arr.filter((x) => `${x.id} ${x.name} ${x.desc} ${(x.tags || []).join(' ')}`.toLowerCase().includes(query)) : arr;
  }, [catalog.skills, query]);

  const filteredAgents = useMemo(() => {
    const arr = catalog.agents;
    return query ? arr.filter((x) => `${x.id} ${x.name} ${x.desc} ${(x.tags || []).join(' ')}`.toLowerCase().includes(query)) : arr;
  }, [catalog.agents, query]);
  const totalSkillsCount = catalog.skills.length;

  // Pagination slice (fall back to the first page when the page number is out of range)
  const pagedSkills = useMemo(
    () => filteredSkills.slice((skillsPage - 1) * SKILLS_PAGE_SIZE, skillsPage * SKILLS_PAGE_SIZE),
    [filteredSkills, skillsPage],
  );
  const pagedAgents = useMemo(
    () => filteredAgents.slice((agentsPage - 1) * SKILLS_PAGE_SIZE, agentsPage * SKILLS_PAGE_SIZE),
    [filteredAgents, agentsPage],
  );

  // Pull back to the first page when search or data changes push the page number out of range
  useEffect(() => {
    const maxPage = Math.max(1, Math.ceil(filteredSkills.length / SKILLS_PAGE_SIZE));
    if (skillsPage > maxPage) setSkillsPage(1);
  }, [filteredSkills.length, skillsPage]);
  useEffect(() => {
    const maxPage = Math.max(1, Math.ceil(filteredAgents.length / SKILLS_PAGE_SIZE));
    if (agentsPage > maxPage) setAgentsPage(1);
  }, [filteredAgents.length, agentsPage]);

  const selectedItem = useMemo(() => {
    if (!selectedId) return null;
    const arr = selectedKind === 'skills' ? catalog.skills : catalog.agents;
    return arr.find((x) => x.id === selectedId) || null;
  }, [selectedId, selectedKind, catalog]);

  useEffect(() => {
    if (embedded) return;
    saveSkillsDetailState(selectedId, selectedKind);
  }, [embedded, selectedId, selectedKind]);

  useEffect(() => {
    if (!selectedId) return;
    if (selectedItem) return;
    setSelectedId(null);
    setSelectedKind('skills');
  }, [selectedId, selectedItem]);

  useEffect(() => {
    if (!embedded) return;
    setSelectedId(null);
    setSelectedKind('skills');
    setSearchVisible(false);
  }, [embedded]);

  useEffect(() => {
    if (panel !== 'skills') return;
    setSelectedId(null);
    setSelectedKind('skills');
    setSearchVisible(false);
  }, [panel, panelEntryNonce]);

  // Return to the first page when the keyword changes
  useEffect(() => {
    setSkillsPage(1);
    setAgentsPage(1);
  }, [query]);

  const toggleEnabled = (kind: PanelKey, id: string, enabled: boolean) => {
    if (!isCatalogKind(kind)) return;
    void toggleItem(kind as 'skills' | 'agents' | 'mcp' | 'kb', id, enabled);
  };

  // Notify parent of detail state changes (covers all code paths including useEffect resets)
  useEffect(() => {
    onDetailChange?.(!!selectedId);
  }, [selectedId, onDetailChange]);

  const openDetail = useCallback((id: string, kind: 'skills' | 'agents') => {
    setNavDir('detail');
    setSelectedId(id);
    setSelectedKind(kind);
  }, []);

  const closeDetail = useCallback(() => {
    setNavDir('list');
    setSelectedId(null);
  }, []);

  // ── Detail View ──────────────────────────────────────────────
  if (selectedItem) {
    // ``detail`` is now the user-facing user_intro markdown (managed via
    // admin DB + configs/user_intros.py defaults). It is NOT the raw
    // SKILL.md body anymore — no frontmatter to parse.
    const markdownBody = (selectedItem as any).detail || '';
    const version = (selectedItem as any).version || '';
    const tags = selectedItem.tags || [];

    return (
      <motion.div
        key="detail"
        className="jx-sk-detailPage"
        {...(navDir === 'detail' ? DRILL_IN_DETAIL : { initial: false })}
      >
        {/* Sticky header: back + name + tag + toggle */}
        <div className="jx-sk-stickyHeader">
          <button className="jx-sk-backBtn jx-sk-backBtn--inline" onClick={closeDetail}>
            <LeftOutlined style={{ fontSize: 14 }} />
          </button>
          <SkillAvatar icon={(selectedItem as any).icon} name={selectedItem.name} seed={selectedItem.id} size={28} round />
          <span className="jx-sk-detailName">{selectedItem.name}</span>
          <Tag className="jx-sk-tag" color={selectedItem.enabled ? 'blue' : 'default'}>
            {selectedItem.enabled ? t('已启用') : t('未启用')}
          </Tag>
          {version && <span className="jx-sk-version" style={{ marginLeft: 4, marginTop: 0, marginBottom: 0 }}>v{version}</span>}
          <div style={{ flex: 1 }} />
          <span className="jx-sk-enableLabel">{t('启用')}</span>
          <Switch
            checked={!!selectedItem.enabled}
            onChange={(v) => toggleEnabled(selectedKind, selectedItem.id, v)}
          />
        </div>

        {/* Scrollable body */}
        <div className="jx-sk-stickyBody">

          {/* Metadata card */}
          <div className="jx-sk-metaCard">
            <h4 className="jx-sk-metaName">{selectedItem.name}</h4>
            <p className="jx-sk-metaDesc">{selectedItem.desc}</p>
            {tags.length > 0 && (
              <div className="jx-sk-metaTags">
                {tags.map((tag: string, i: number) => (
                  <Tag key={i} className="jx-sk-metaTag">{tag}</Tag>
                ))}
              </div>
            )}
          </div>

          {/* Body: markdown content */}
          <div className="jx-sk-detailBody">
            {markdownBody ? (
              <div className="jx-md jx-sk-detailMarkdown" dangerouslySetInnerHTML={{ __html: mdToHtml(markdownBody) }} />
            ) : (
              <Typography.Text type="secondary">{t('暂无详情')}</Typography.Text>
            )}
          </div>
        </div>
      </motion.div>
    );
  }

  // ── List View ────────────────────────────────────────────────
  return (
    <motion.div
      key="list"
      className="jx-sk-page"
      {...(navDir === 'list' ? DRILL_IN_BACK : { initial: false })}
    >
      {/* Header */}
      <div className="jx-sk-header">
        <div>
          <h2 className="jx-sk-title">
            {skillsTitle}
            <span className="jx-sectionTitleCount">{t('（共 {n} 项）', { n: totalSkillsCount })}</span>
          </h2>
          {skillsSubtitle ? <p className="jx-sk-subtitle">{skillsSubtitle}</p> : null}
        </div>
        <div className="jx-sk-headerRight">
          {searchVisible ? (
            <Input
              allowClear
              placeholder={t('搜索技能关键词')}
              className="jx-mcp-searchInput"
              value={manageQuery}
              onChange={(e) => setManageQuery(e.target.value)}
              autoFocus
              onBlur={() => { if (!manageQuery) setSearchVisible(false); }}
            />
          ) : (
            <div className="jx-mcp-searchBox" onClick={() => setSearchVisible(true)}>
              <SearchOutlined style={{ color: '#B3B3B3', fontSize: 14 }} />
              <span className="jx-mcp-searchPlaceholder">{t('搜索技能关键词')}</span>
            </div>
          )}
          {canAddSkill && (
            <>
              <input
                ref={fileInputRef}
                type="file"
                accept=".zip"
                style={{ display: 'none' }}
                onChange={handleUploadSkill}
              />
              <Dropdown
                menu={{
                  items: [
                    { key: 'market', icon: <AppstoreAddOutlined />, label: t('从技能市场获取'), onClick: () => setMarketplaceOpen(true) },
                    { key: 'write', icon: <EditOutlined />, label: t('手写新建技能'), onClick: openCreateSkill },
                    { key: 'upload', icon: <UploadOutlined />, label: t('上传技能包（zip）'), onClick: () => fileInputRef.current?.click() },
                  ],
                }}
              >
                <Button type="primary" icon={<PlusOutlined />} loading={uploading} style={{ marginLeft: 8 }}>
                  {t('添加技能')} <DownOutlined />
                </Button>
              </Dropdown>
            </>
          )}
        </div>
      </div>

      {/* Section 1: Skills — card grid (the container key controls stagger replay: replay on entering the panel/paging, no replay on toggle optimistic updates) */}
      <div
        className="jx-sk-grid jx-anim-stagger"
        style={{ '--stagger-step': '30ms' } as React.CSSProperties}
        key={`sk-${panelEntryNonce}-${skillsPage}`}
      >
        {pagedSkills.map((item, idx) => (
          <div
            key={item.id}
            className="jx-sk-card jx-card-lift"
            style={staggerStyle(idx)}
            onClick={() => openDetail(item.id, 'skills')}
          >
            <div className="jx-sk-cardTop">
              <SkillAvatar icon={(item as any).icon} name={item.name} seed={item.id} size={28} round />
              <div className="jx-sk-cardNameGroup">
                <span className="jx-sk-cardName">{item.name}</span>
                {item.enabled && (
                  <Tag className="jx-sk-tag" color="blue">{t('已启用')}</Tag>
                )}
                {item.owner === 'self' && (
                  <Tag style={{ background: '#EBF2FF', color: '#126DFF', border: 'none' }}>{t('我的')}</Tag>
                )}
                {item.owner === 'self' && (() => {
                  const sub = subBySkill.get(item.id);
                  if (!sub) return null;
                  // key=status: remounts on status flip, plays the statusIn settle animation once
                  if (sub.status === 'pending') return <Tag key="pending" className="jx-anim-statusIn" color="gold" bordered={false}>{t('上架审核中')}</Tag>;
                  if (sub.status === 'approved') return <Tag key="approved" className="jx-anim-statusIn" color="green" bordered={false}>{t('已上架市场')}</Tag>;
                  return (
                    <Tooltip key="rejected" title={sub.review_note ? `驳回理由：${sub.review_note}` : '申请被驳回，可调整后重新申请'}>
                      <Tag className="jx-anim-statusIn" color="red" bordered={false}>{t('上架被驳回')}</Tag>
                    </Tooltip>
                  );
                })()}
              </div>
              {item.owner === 'self' && (
                // Note: Popconfirm's confirm button renders in a portal, but React synthetic events bubble along the component tree,
                // so they still reach the card onClick through this container. Only stopPropagation at the container level can block "clicking confirm jumps to detail".
                <div className="jx-sk-cardActions" style={{ marginLeft: 'auto', display: 'flex', gap: 2 }}
                  onClick={(e) => e.stopPropagation()}>
                  {(() => {
                    const sub = subBySkill.get(item.id);
                    if (sub?.status === 'pending') {
                      return (
                        <Popconfirm
                          title={t('撤回上架申请？')}
                          okText={t('撤回')}
                          cancelText={t('取消')}
                          onConfirm={() => handleWithdraw(sub.submission_id)}
                        >
                          <Button
                            type="text"
                            size="small"
                            icon={<CloudUploadOutlined />}
                            title={t('上架审核中，点击撤回申请')}
                            onClick={(e) => e.stopPropagation()}
                          />
                        </Popconfirm>
                      );
                    }
                    if (sub?.status === 'approved') {
                      return (
                        <Button
                          type="text"
                          size="small"
                          icon={<CloudUploadOutlined style={{ color: '#02B589' }} />}
                          title={t('已上架技能市场，如需下架请联系管理员')}
                          onClick={(e) => e.stopPropagation()}
                        />
                      );
                    }
                    return (
                      <Button
                        type="text"
                        size="small"
                        icon={<CloudUploadOutlined />}
                        title={sub?.status === 'rejected' ? t('重新申请上架') : t('申请上架技能市场')}
                        onClick={(e) => { e.stopPropagation(); openApply(item.id); }}
                      />
                    );
                  })()}
                  <Button
                    type="text"
                    size="small"
                    icon={<EditOutlined />}
                    title={t('编辑技能')}
                    onClick={(e) => { e.stopPropagation(); void handleEditSkill(item.id); }}
                  />
                  <Button
                    type="text"
                    size="small"
                    icon={<DownloadOutlined />}
                    title={t('导出技能包（zip）')}
                    onClick={(e) => { e.stopPropagation(); void handleExportSkill(item.id); }}
                  />
                  <Popconfirm
                    title={t('删除这个私有技能？')}
                    okText={t('删除')}
                    cancelText={t('取消')}
                    okButtonProps={{ danger: true }}
                    onConfirm={() => handleDeleteSkill(item.id)}
                  >
                    <Button
                      type="text"
                      size="small"
                      danger
                      icon={<DeleteOutlined />}
                      onClick={(e) => e.stopPropagation()}
                    />
                  </Popconfirm>
                </div>
              )}
            </div>
            <div className="jx-sk-cardDesc">{item.desc}</div>
          </div>
        ))}
      </div>

      {filteredSkills.length === 0 && (
        <div className="jx-anim-fadeIn" style={{ padding: '40px 0', textAlign: 'center' }}>
          <Typography.Text type="secondary">{t('没有匹配的技能')}</Typography.Text>
        </div>
      )}

      {filteredSkills.length > SKILLS_PAGE_SIZE && (
        <div className="jx-sk-pagination">
          <Pagination
            current={skillsPage}
            pageSize={SKILLS_PAGE_SIZE}
            total={filteredSkills.length}
            onChange={setSkillsPage}
            showSizeChanger={false}
            size="small"
          />
        </div>
      )}

      {/* Section 2: Agents — card grid */}
      {filteredAgents.length > 0 && (
        <>
          <div
            className="jx-sk-grid jx-sk-grid--agents jx-anim-stagger"
            style={{ '--stagger-step': '30ms' } as React.CSSProperties}
            key={`ag-${panelEntryNonce}-${agentsPage}`}
          >
            {pagedAgents.map((item, idx) => (
              <div
                key={item.id}
                className="jx-sk-card jx-card-lift"
                style={staggerStyle(idx)}
                onClick={() => openDetail(item.id, 'agents')}
              >
                <div className="jx-sk-cardTop">
                  <SkillAvatar icon={(item as any).icon} name={item.name} seed={item.id} size={28} round />
                  <div className="jx-sk-cardNameGroup">
                    <span className="jx-sk-cardName">{item.name}</span>
                    {item.enabled && (
                      <Tag className="jx-sk-tag" color="blue">{t('已启用')}</Tag>
                    )}
                  </div>
                </div>
                <div className="jx-sk-cardDesc">{item.desc}</div>
              </div>
            ))}
          </div>

          {filteredAgents.length > SKILLS_PAGE_SIZE && (
            <div className="jx-sk-pagination">
              <Pagination
                current={agentsPage}
                pageSize={SKILLS_PAGE_SIZE}
                total={filteredAgents.length}
                onChange={setAgentsPage}
                showSizeChanger={false}
                size="small"
              />
            </div>
          )}
        </>
      )}

      {/* Hand-write new / edit skill modal */}
      <Modal
        title={editingSkillId ? t('编辑技能') : t('手写新建技能')}
        open={handwriteOpen}
        onCancel={() => { setHandwriteOpen(false); setEditingSkillId(null); resetSkillFileState(); }}
        onOk={() => void handleCreateSkill()}
        okText={editingSkillId ? t('保存') : t('创建')}
        cancelText={t('取消')}
        confirmLoading={creatingSkill}
        okButtonProps={{ disabled: loadingSkill }}
        width={640}
        destroyOnHidden
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          {editingSkillId
            ? t('修改技能信息后保存。技能 ID 不可更改，该技能仅你自己可见可用。')
            : t('直接填写技能信息即可创建，无需打包 zip。该技能仅你自己可见可用。')}
        </Typography.Paragraph>
        <Form form={skillForm} layout="vertical">
          <Form.Item label={t('图标')}>
            <SkillIconPicker
              value={skillIcon}
              name={skillForm.getFieldValue('display_name')}
              seed={editingSkillId || skillForm.getFieldValue('name') || ''}
              onChange={setSkillIcon}
            />
          </Form.Item>
          <Form.Item
            name="name"
            label={t('技能 ID')}
            rules={[
              { required: true, message: t('请输入技能 ID') },
              { pattern: /^[a-z0-9_-]{1,63}$/, message: t('仅小写字母、数字、- 和 _，最长 63 位') },
            ]}
          >
            <Input placeholder="如 my-weather-helper" disabled={!!editingSkillId} />
          </Form.Item>
          <Form.Item name="display_name" label={t('名称')} rules={[{ required: true, message: t('请输入名称') }]}>
            <Input placeholder={t('名称')} maxLength={255} />
          </Form.Item>
          <Form.Item
            name="description"
            label={t('一句话描述')}
            rules={[{ required: true, message: t('请输入描述，描述为空时技能无法被识别调用') }]}
            tooltip={t('描述是技能被智能体识别、检索的依据，不能为空')}
          >
            <Input placeholder={t('这个技能是做什么的、什么时候用')} maxLength={2000} />
          </Form.Item>
          <Form.Item
            name="tags"
            label={t('普通标签（可选）')}
          >
            <Select mode="tags" placeholder={t('回车添加标签')} tokenSeparators={[',']} />
          </Form.Item>
          <Form.Item
            name="ontology_tags"
            label={t('本体治理标签')}
            tooltip={t('标签来自当前激活领域包；实际调用技能时，会触发标签关联的本体工作流和评审级别。')}
          >
            <OntologyTagSelect options={ontologyTagOptions} loading={ontologyTagsLoading} />
          </Form.Item>
          <Form.Item
            name="mcp_server_ids"
            label={t('绑定工具 (MCP)')}
            tooltip={t('选择技能执行时依赖的 MCP。系统会读取 MCP 的实际工具清单，用于本体构建校验。')}
            extra={t('如果本体流程要求特定工具，请在这里绑定对应 MCP；绑定信息和工具清单会写入 SKILL.md。')}
          >
            <Select
              mode="multiple"
              allowClear
              showSearch
              optionFilterProp="label"
              maxTagCount="responsive"
              placeholder={t('选择可用的 MCP 工具')}
              options={skillMcpOptions}
            />
          </Form.Item>
          <Form.Item name="instructions" label={t('技能正文（指令）')} rules={[{ required: true, message: t('请输入技能正文') }]}>
            <Input.TextArea rows={10} placeholder={t('用 Markdown 写清楚这个技能怎么用、步骤、注意事项……')} />
          </Form.Item>
        </Form>

        {/* Skill file management —— shown only when editing an existing skill (for new skills, create first, then add files) */}
        {editingSkillId && (
          <div style={{ marginTop: 8, borderTop: '1px solid var(--color-border, #E3E6EA)', paddingTop: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
              <Typography.Text strong>{t('技能文件（{n}）', { n: skillFiles.length })}</Typography.Text>
              <div style={{ display: 'flex', gap: 8 }}>
                <input
                  ref={skillFileInputRef}
                  type="file"
                  multiple
                  style={{ display: 'none' }}
                  onChange={handleUploadSkillFile}
                />
                <Button size="small" icon={<UploadOutlined />} onClick={() => skillFileInputRef.current?.click()}>
                  {t('上传文件')}
                </Button>
              </div>
            </div>
            <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
              {t('技能文件夹内 SKILL.md 以外的文件（脚本、模板、配置等），保存即时生效。二进制文件仅支持上传/删除。')}
            </Typography.Paragraph>
            {skillFiles.length > 0 && (
              <div style={{ border: '1px solid var(--color-border, #E3E6EA)', borderRadius: 8, marginBottom: 8, maxHeight: 200, overflowY: 'auto' }}>
                {skillFiles.map((f) => (
                  <div
                    key={f.filename}
                    style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 12px', borderBottom: '1px solid var(--color-bg-gray, #F5F6F7)' }}
                  >
                    <FileTextOutlined style={{ color: 'var(--color-text-tertiary, #808080)' }} />
                    <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 13 }}>{f.filename}</span>
                    <span style={{ fontSize: 12, color: 'var(--color-text-placeholder, #B3B3B3)' }}>
                      {f.size >= 1024 ? `${(f.size / 1024).toFixed(1)} KB` : `${f.size} B`}
                    </span>
                    {f.is_binary ? (
                      <Tag style={{ margin: 0 }}>{t('二进制')}</Tag>
                    ) : (
                      <Button type="link" size="small" icon={<EditOutlined />} onClick={() => void openSkillFile(f.filename, f.is_binary)}>
                        {t('编辑')}
                      </Button>
                    )}
                    <Popconfirm title={t('确定删除该文件？')} okText={t('删除')} cancelText={t('取消')} okButtonProps={{ danger: true }} onConfirm={() => void handleDeleteSkillFile(f.filename)}>
                      <Button type="link" size="small" danger icon={<DeleteOutlined />} />
                    </Popconfirm>
                  </div>
                ))}
              </div>
            )}
            <div style={{ display: 'flex', gap: 8 }}>
              <Input
                size="small"
                placeholder={t('新建文件名，如 scripts/run.py / config.json')}
                value={newSkillFileName}
                onChange={(e) => setNewSkillFileName(e.target.value)}
                onPressEnter={handleCreateSkillFile}
                style={{ width: 280 }}
              />
              <Button size="small" icon={<PlusOutlined />} onClick={handleCreateSkillFile}>{t('新建文件')}</Button>
            </div>
          </div>
        )}
      </Modal>

      <OntologyBuildValidationModal failure={buildFailure} onClose={() => setBuildFailure(null)} />

      {/* Skill file editor modal —— separate from the edit-skill modal to avoid stretching the card too long */}
      <Modal
        title={editingFileName || ''}
        open={!!editingFileName}
        onCancel={() => { setEditingFileName(null); setEditingFileContent(''); }}
        onOk={() => void handleSaveSkillFile()}
        okText={t('保存文件')}
        okButtonProps={{ icon: <SaveOutlined />, loading: fileSaving }}
        cancelText={t('取消')}
        width="min(760px, calc(100vw - 32px))"
        style={{ top: 40 }}
        destroyOnHidden
      >
        <Input.TextArea
          value={editingFileContent}
          onChange={(e) => setEditingFileContent(e.target.value)}
          rows={20}
          disabled={fileLoading}
          placeholder={fileLoading ? t('加载中…') : undefined}
          style={{ fontFamily: 'monospace', fontSize: 13 }}
        />
      </Modal>

      {/* Skill marketplace modal —— install as a private skill */}
      <SkillMarketplaceModal
        open={marketplaceOpen}
        onClose={() => setMarketplaceOpen(false)}
        fetchers={marketplaceFetchers}
        scopeLabel={t('仅自己可见可用')}
        onInstalled={() => { void fetchCatalog(); }}
      />

      {/* Apply-to-list-on-marketplace modal */}
      <Modal
        title={t('申请上架技能市场')}
        open={!!applySkillId}
        onCancel={() => setApplySkillId(null)}
        onOk={() => void handleApply()}
        okText={t('提交申请')}
        cancelText={t('取消')}
        confirmLoading={applying}
        width={520}
        destroyOnHidden
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          {t('提交后由管理员审核，通过后该技能将出现在技能市场，所有用户都可以安装使用。审核基于当前内容的快照，之后你对原技能的修改不会影响已上架版本。')}
        </Typography.Paragraph>
        {applySkillId && subBySkill.get(applySkillId)?.status === 'rejected' && (
          <Typography.Paragraph type="danger" style={{ fontSize: 12 }}>
            上次申请被驳回{subBySkill.get(applySkillId)?.review_note ? `：${subBySkill.get(applySkillId)?.review_note}` : ''}，建议调整后重新提交。
          </Typography.Paragraph>
        )}
        <Form form={applyForm} layout="vertical">
          <Form.Item name="summary" label={t('市场展示摘要（可选）')} tooltip={t('留空则使用技能的一句话描述')}>
            <Input.TextArea rows={2} maxLength={2000} placeholder={t('向其他用户介绍这个技能能做什么')} />
          </Form.Item>
          <Form.Item name="category" label={t('上架分类')} rules={[{ required: true, message: t('请选择上架分类') }]}>
            <Select
              placeholder={t('选择该技能在市场中的分类')}
              options={MARKETPLACE_CATEGORIES.map((c) => ({ value: c, label: c }))}
            />
          </Form.Item>
          <Form.Item name="note" label={t('给管理员的备注（可选）')}>
            <Input.TextArea rows={3} maxLength={2000} placeholder={t('补充说明使用场景、测试情况等，便于审核')} />
          </Form.Item>
        </Form>
      </Modal>
    </motion.div>
  );
}
