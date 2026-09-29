import { useEffect, useState } from 'react';
import { Button, Popover, Switch, Tooltip, message } from 'antd';
import { SettingOutlined } from '@ant-design/icons';
import { useProjectStore } from '../../stores/projectStore';
import ProjectMemoriesModal from './ProjectMemoriesModal';
import { t } from '../../i18n';

export default function ProjectMemoryCard({ projectId }: { projectId: string }) {
  const project = useProjectStore((s) => s.currentProject);
  const updateProject = useProjectStore((s) => s.updateProject);
  const readEnabled = project?.memory_enabled ?? true;
  const writeEnabled = project?.memory_write_enabled ?? true;
  const canEdit = project?.permission === 'admin' || project?.permission === 'edit';

  const [count, setCount] = useState<number | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [savingRead, setSavingRead] = useState(false);
  const [savingWrite, setSavingWrite] = useState(false);
  const [viewerOpen, setViewerOpen] = useState(false);

  useEffect(() => {
    let aborted = false;
    void (async () => {
      try {
        const { getApiUrl } = await import('../../api');
        const resp = await fetch(
          `${getApiUrl()}/v1/memories?project_id=${encodeURIComponent(projectId)}`,
          { credentials: 'include' },
        );
        const payload = await resp.json();
        if (aborted) return;
        const data = payload?.data || {};
        setCount(typeof data.count === 'number' ? data.count : 0);
      } catch {
        if (!aborted) setCount(0);
      }
    })();
    return () => { aborted = true; };
  }, [projectId, reloadKey, readEnabled]);

  const toggle = async (kind: 'read' | 'write', next: boolean) => {
    const setSaving = kind === 'read' ? setSavingRead : setSavingWrite;
    setSaving(true);
    try {
      await updateProject(
        kind === 'read' ? { memory_enabled: next } : { memory_write_enabled: next },
      );
      setReloadKey((k) => k + 1);
    } catch (err) {
      message.error((err as Error)?.message || t('保存失败'));
    } finally {
      setSaving(false);
    }
  };

  const settingsContent = (
    <div className="jx-projectRail-memoryToggles">
      <Tooltip title={t('关闭后，本项目内对话不会检索 / 注入项目记忆')} placement="left">
        <div className="jx-projectRail-memoryToggleRow">
          <span className="jx-projectRail-memoryToggleLabel">{t('读取记忆')}</span>
          <Switch
            size="small"
            checked={readEnabled}
            loading={savingRead}
            disabled={!canEdit}
            onChange={(v) => toggle('read', v)}
          />
        </div>
      </Tooltip>
      <Tooltip title={t('关闭后，本项目内会话结束不会抽取并写入新的项目记忆')} placement="left">
        <div className="jx-projectRail-memoryToggleRow">
          <span className="jx-projectRail-memoryToggleLabel">{t('写入记忆')}</span>
          <Switch
            size="small"
            checked={writeEnabled}
            loading={savingWrite}
            disabled={!canEdit}
            onChange={(v) => toggle('write', v)}
          />
        </div>
      </Tooltip>
    </div>
  );

  return (
    <div className="jx-projectRail-card">
      <div className="jx-projectRail-cardHeader">
        <div className="jx-projectRail-cardTitle">{t('项目记忆')}</div>
        <div className="jx-projectRail-cardHeaderRight">
          <span className="jx-projectRail-cardAux">{t('仅本项目可见')}</span>
          <Popover
            content={settingsContent}
            title={t('项目记忆设置')}
            trigger="click"
            placement="bottomRight"
            overlayClassName="jx-projectRail-memoryPopover"
          >
            <Button
              type="text"
              size="small"
              icon={<SettingOutlined />}
              title={t('项目记忆设置')}
            />
          </Popover>
        </div>
      </div>

      {!readEnabled ? (
        <div className="jx-projectRail-cardEmpty">{t('读取已关闭，项目记忆不会注入对话')}</div>
      ) : count === null ? (
        <div className="jx-projectRail-cardEmpty">{t('加载中…')}</div>
      ) : count === 0 ? (
        <div className="jx-projectRail-cardEmpty">{t('几轮对话之后，项目记忆会出现在这里。')}</div>
      ) : (
        <div
          className="jx-projectRail-cardEmpty jx-projectRail-memoryCount"
          onClick={() => setViewerOpen(true)}
          style={{ cursor: 'pointer' }}
          title={t('点击查看项目记忆详情')}
        >
          {t('已积累 {n} 条记忆 · ', { n: count })}<span style={{ color: 'var(--color-primary)' }}>{t('查看')}</span>
        </div>
      )}

      <ProjectMemoriesModal
        open={viewerOpen}
        projectId={projectId}
        projectName={project?.name}
        onClose={() => setViewerOpen(false)}
        onChange={() => setReloadKey((k) => k + 1)}
      />
    </div>
  );
}
