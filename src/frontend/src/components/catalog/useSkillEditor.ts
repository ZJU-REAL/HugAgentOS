import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Form, message } from 'antd';
import { t } from '../../i18n';
import { useAgentStore, useCatalogStore } from '../../stores';
import type { OntologyTagOption } from '../../types';
import { createMySkill, getMySkill, getMySkillFile, saveMySkillFile, deleteMySkillFile, uploadMySkillFile, exportMySkillZip, getOntologyTagOptions, type MySkillFileInfo } from '../../api';
import { getOntologyBuildFailure, type OntologyBuildFailure } from '../../utils/apiError';

export function useSkillEditor(canAddSkill: boolean) {
  const fetchCatalog = useCatalogStore((s) => s.fetchCatalog);
  const availableResources = useAgentStore((s) => s.availableResources);
  const fetchAvailableResources = useAgentStore((s) => s.fetchAvailableResources);
  const [handwriteOpen, setHandwriteOpen] = useState(false);
  const [creatingSkill, setCreatingSkill] = useState(false);
  // null = create mode; non-null = the skill id being edited (the skill id cannot be changed, disabled in the form)
  const [editingSkillId, setEditingSkillId] = useState<string | null>(null);
  const [editingRevision, setEditingRevision] = useState<string | undefined>();
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
    setEditingRevision(undefined);
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
      setEditingRevision((detail as typeof detail & { revision?: string }).revision);
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
      const input = {
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
        expected_revision: editingRevision,
      };
      await createMySkill(input);
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
  }, [skillForm, fetchCatalog, editingSkillId, editingRevision, skillIcon, resetSkillFileState]);

  return { handwriteOpen, setHandwriteOpen, creatingSkill, editingSkillId, setEditingSkillId, loadingSkill, skillIcon, setSkillIcon, skillForm, ontologyTagOptions, ontologyTagsLoading, buildFailure, setBuildFailure, skillMcpOptions, skillFiles, editingFileName, setEditingFileName, editingFileContent, setEditingFileContent, fileLoading, fileSaving, newSkillFileName, setNewSkillFileName, skillFileInputRef, resetSkillFileState, openCreateSkill, handleEditSkill, openSkillFile, handleSaveSkillFile, handleDeleteSkillFile, handleUploadSkillFile, handleCreateSkillFile, handleExportSkill, handleCreateSkill };
}
