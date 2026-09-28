import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { motion } from 'motion/react';
import { Alert, Tag, Input, Typography, Button, Popconfirm, message, Dropdown, Pagination, Tooltip } from 'antd';
import { t } from '../../i18n';
import { stripMarkdown } from '../../utils/markdown';
import { DeviceCapabilityBadge } from './DeviceCapabilityBadge';
import { useDesktopCapabilityStore } from '../../stores/desktopCapabilityStore';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { mergeDeviceSkills } from '../../utils/deviceSkillCatalog';
import { SearchOutlined, PlusOutlined, DeleteOutlined, UploadOutlined, EditOutlined, DownOutlined, AppstoreAddOutlined, CloudUploadOutlined, DownloadOutlined } from '@ant-design/icons';
import { useCatalogStore, useAuthStore } from '../../stores';
import { usePanel } from '../../routing/usePanel';
import type { PanelKey } from '../../types';
import { isCatalogKind } from '../../utils/constants';
import { staggerStyle } from '../../utils/motionTokens';
import { DRILL_IN_BACK } from '../../utils/motionVariants';
import { usePanelHeader } from '../../hooks/usePageConfig';
import { ABILITY_TAB_TITLE } from './abilityTabs';
import { deleteMySkill, uploadMySkill } from '../../api';
import { SkillAvatar } from './skillIcons';
import { CardTail } from '../common/CardTail';
import { useSkillEditor } from './useSkillEditor';
import { SkillEditorDialogs } from './SkillEditorDialogs';
import { SkillLibraryDetail } from './SkillLibraryDetail';
import { useSkillMarketplace } from './useSkillMarketplace';
import { SkillMarketplaceDialogs } from './SkillMarketplaceDialogs';

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

export function SkillsPage({ embedded = false }: { embedded?: boolean }) {
  const deviceSkills = useDesktopCapabilityStore((s) => s.kinds.skill.items);
  const discoveryErrors = useDesktopCapabilityStore((s) => s.kinds.skill.discoveryErrors);
  const dual = useDeploymentModeStore((s) => s.provisionMode === 'dual');
  const panel = usePanel();
  const {
    catalog,
    panelEntryNonce,
    manageQuery, setManageQuery,
    toggleItem,
  } = useCatalogStore();
  const { title: skillsTitle, subtitle: skillsSubtitle } = usePanelHeader('skills', {
    title: ABILITY_TAB_TITLE.skills,
    subtitle: '启用/停用技能，并查看详细介绍、输入输出与示例',
  });

  const fetchCatalog = useCatalogStore((s) => s.fetchCatalog);
  const canAddSkill = useAuthStore((s) => s.authUser?.can_add_skill === true);

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

  const marketplace = useSkillMarketplace(canAddSkill);
  const { setMarketplaceOpen, subBySkill, openApply, handleWithdraw } = marketplace;

  const editor = useSkillEditor(canAddSkill);
  const { openCreateSkill, handleEditSkill, handleExportSkill } = editor;

  const query = manageQuery.trim().toLowerCase();

  const allSkills = useMemo(() => dual ? mergeDeviceSkills(catalog.skills, deviceSkills) : catalog.skills, [catalog.skills, deviceSkills, dual]);
  const filteredSkills = useMemo(() => {
    const arr = allSkills;
    return query ? arr.filter((x) => `${x.id} ${x.name} ${x.desc} ${(x.tags || []).join(' ')}`.toLowerCase().includes(query)) : arr;
  }, [allSkills, query]);

  const filteredAgents = useMemo(() => {
    const arr = catalog.agents;
    return query ? arr.filter((x) => `${x.id} ${x.name} ${x.desc} ${(x.tags || []).join(' ')}`.toLowerCase().includes(query)) : arr;
  }, [catalog.agents, query]);
  const totalSkillsCount = allSkills.length;

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

  const openDetail = useCallback((id: string, kind: 'skills' | 'agents') => {
    setNavDir('detail');
    setSelectedId(id);
    setSelectedKind(kind);
  }, []);

  const closeDetail = useCallback(() => {
    setNavDir('list');
    setSelectedId(null);
  }, []);

  if (selectedItem) return <SkillLibraryDetail selectedItem={selectedItem} selectedKind={selectedKind}
    navDir={navDir} closeDetail={closeDetail} toggleEnabled={toggleEnabled} />;

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
      {dual && discoveryErrors.map((error) => <Alert key={error.folder} type="warning" showIcon
        message={error.folder} style={{ marginBottom: 12 }}
        description={t(error.code === 'name_conflict' ? '技能标识已存在，未覆盖原技能' : '技能文件夹无效或尚未复制完成')} />)}
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
            onClick={() => { if (item.owner !== 'device') openDetail(item.id, 'skills'); }}
          >
            <div className="jx-sk-cardTop">
              <SkillAvatar icon={'icon' in item ? String(item.icon || '') : undefined} name={item.name} seed={item.id} size={28} round />
              <div className="jx-sk-cardNameGroup">
                <span className="jx-sk-cardName">{item.name}</span>
                <DeviceCapabilityBadge kind="skill" runtimeName={item.id} />
                {item.owner === 'self' && (
                  <Tag style={{ background: 'var(--color-primary-light)', color: 'var(--color-primary)', border: 'none' }}>{t('我的')}</Tag>
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
              {item.owner === 'device' ? <Tag>{t(item.enabled ? '已启用' : '已停用')}</Tag> : <CardTail
                checked={!!item.enabled}
                onChange={(v) => toggleEnabled('skills', item.id, v)}
                actions={item.owner === 'self' && (
                  <>
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
                  </>
                )}
              />}
            </div>
            <div className="jx-sk-cardDesc">{stripMarkdown(item.desc)}</div>
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
                  <SkillAvatar icon={'icon' in item ? String(item.icon || '') : undefined} name={item.name} seed={item.id} size={28} round />
                  <div className="jx-sk-cardNameGroup">
                    <span className="jx-sk-cardName">{item.name}</span>
                    <DeviceCapabilityBadge kind="agent" runtimeName={item.name} />
                  </div>
                  <CardTail
                    checked={!!item.enabled}
                    onChange={(v) => toggleEnabled('agents', item.id, v)}
                  />
                </div>
                <div className="jx-sk-cardDesc">{stripMarkdown(item.desc)}</div>
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

      <SkillEditorDialogs editor={editor} />

      <SkillMarketplaceDialogs marketplace={marketplace} fetchCatalog={fetchCatalog} />
    </motion.div>
  );
}
