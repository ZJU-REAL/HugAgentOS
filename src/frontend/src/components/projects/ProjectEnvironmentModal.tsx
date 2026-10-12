import { useEffect, useState } from 'react';
import { Alert, Button, Modal, Spin, message } from 'antd';
import {
  defaultProjectEnvironment, getProjectEnvironment, saveProjectEnvironment,
  type ProjectEnvironment,
} from '../../api/projectEnvironment';
import ProjectEnvironmentEditor from './ProjectEnvironmentEditor';
import { t } from '../../i18n';

export default function ProjectEnvironmentModal({ projectId, open, onClose }: {
  projectId: string; open: boolean; onClose: () => void;
}) {
  const [config, setConfig] = useState<ProjectEnvironment>(defaultProjectEnvironment);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const [ready, setReady] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open) return;
    let active = true;
    setLoading(true); setError(''); setReady(false);
    void getProjectEnvironment(projectId).then(detail => {
      if (active) setConfig(detail.config);
    }).catch(err => {
      if (active) setError(err.message || t('加载项目环境失败'));
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [projectId, open, retry]);

  const save = async () => {
    setSaving(true);
    try {
      await saveProjectEnvironment(projectId, config);
      message.success(t('项目环境已更新，从下一轮对话生效'));
      onClose();
    } catch (err) {
      message.error((err as Error).message || t('保存项目环境失败'));
    } finally { setSaving(false); }
  };

  return <Modal title={t('绑定项目环境')} open={open} onCancel={onClose} width={640}
    onOk={() => void save()} okText={t('保存环境')} cancelText={t('取消')}
    confirmLoading={saving} okButtonProps={{ disabled: loading || !!error || !ready }}>
    <p style={{ color: 'var(--color-text-secondary)' }}>
      {t('所有项目对话共用此环境。正在执行的对话保持原环境，项目外对话不受影响。')}
    </p>
    {error ? <Alert type="error" message={error}
      action={<Button onClick={() => setRetry(v => v + 1)}>{t('重试')}</Button>} /> :
      loading ? <Spin /> : <ProjectEnvironmentEditor value={config} onChange={setConfig}
        projectId={projectId} onReadyChange={setReady} />}
  </Modal>;
}
