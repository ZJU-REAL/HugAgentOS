import { useEffect, useRef, useState } from 'react';
import { Input, Modal, Tooltip, message } from 'antd';
import { useProjectStore } from '../../stores/projectStore';
import { ProjectVisibilityEditor } from '../../projectEdition';
import type { EditionProjectUpdateFields } from '../../editionModelTypes';
import { t } from '../../i18n';

interface Props {
  open: boolean;
  onClose: () => void;
}

/** 编辑当前项目的名称 / 项目目标 / 可见范围。
 *
 *  后端 `PATCH /v1/projects/{id}` 按字段分权：`name` 需要 admin，`description` 只要 edit，
 *  可见范围只有项目创建人或团队所有者（`is_owner`）能改 —— 界面按同样的规则置灰或隐藏。
 */
export default function EditProjectModal({ open, onClose }: Props) {
  const project = useProjectStore((s) => s.currentProject);
  const updateProject = useProjectStore((s) => s.updateProject);
  const canRename = project?.permission === 'admin';

  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  // Only read at submit time, so a ref avoids re-rendering the whole modal per change.
  const visibilityPatch = useRef<EditionProjectUpdateFields>({});
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!open || !project) return;
    setName(project.name || '');
    setDescription(project.description || '');
  }, [open, project]);

  const handleSubmit = async () => {
    if (!project) return;
    const cleanName = name.trim();
    if (!cleanName) {
      message.warning(t('请填写项目名'));
      return;
    }
    const patch: Parameters<typeof updateProject>[0] = { ...visibilityPatch.current };
    if (canRename && cleanName !== project.name) patch.name = cleanName;
    if (description.trim() !== (project.description || '')) patch.description = description.trim();
    if (Object.keys(patch).length === 0) {
      onClose();
      return;
    }
    setSubmitting(true);
    try {
      await updateProject(patch);
      message.success(t('项目信息已更新'));
      onClose();
    } catch (err) {
      message.error((err as Error)?.message || t('保存失败'));
    } finally {
      setSubmitting(false);
    }
  };

  if (!project) return null;

  return (
    <Modal
      title={t('编辑项目信息')}
      open={open}
      onCancel={onClose}
      onOk={handleSubmit}
      okText={t('保存')}
      cancelText={t('取消')}
      confirmLoading={submitting}
      destroyOnClose
      width={520}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 16, paddingTop: 8 }}>
        <div>
          <div style={{ marginBottom: 6, fontWeight: 600, fontSize: 13 }}>
            {t('项目名称')} <span style={{ color: 'red' }}>*</span>
          </div>
          <Tooltip title={canRename ? '' : t('仅项目管理员可修改项目名称')}>
            <Input
              placeholder={t('给项目起个名字')}
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={120}
              disabled={!canRename}
              autoFocus={canRename}
              onPressEnter={() => void handleSubmit()}
            />
          </Tooltip>
        </div>

        <div>
          <div
            style={{
              marginBottom: 6,
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              fontSize: 13,
            }}
          >
            <span style={{ fontWeight: 600 }}>{t('项目目标')}</span>
            <span style={{ color: 'var(--color-text-tertiary, #808080)', fontSize: 12 }}>
              {description.length}/2000
            </span>
          </div>
          <Input.TextArea
            placeholder={t('简述这个项目的目标、主题或上下文…')}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={4}
            maxLength={2000}
          />
        </div>

        <ProjectVisibilityEditor project={project} onPatchChange={(p) => { visibilityPatch.current = p; }} />
      </div>
    </Modal>
  );
}
