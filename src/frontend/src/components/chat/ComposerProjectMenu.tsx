import { useProjectStore } from '../../stores/projectStore';
import CreateProjectModal from '../projects/CreateProjectModal';
import { Dropdown } from 'antd';
import { FolderOutlined, FolderOpenOutlined, FolderAddOutlined, LaptopOutlined } from '@ant-design/icons';
import { ChipChevron } from '../common/ChipChevron';
import { t } from '../../i18n';
import type { ComposerState } from './useComposerState';

export function ComposerProjectMenu({ state }: { state: ComposerState }) {
  const { bindChatProject, boundProjectId, unbindChatProject, currentChatId, projects, onPickProject, isDesktopShell,
    canCreateCloudProject, canCreateLocalProject, setProjectCreateModalOpen,
    setProjectOpen, fetchProjects, projectOpen, boundProjectName } = state;
  const projectMenuItems = [
    {
      key: 'proj-group',
      type: 'group' as const,
      label: t('项目'),
      children: [
        {
          key: 'proj-default',
          label: (
            <div className="jx-projectOption">
              <FolderOutlined className="jx-projectOptionIcon" />
              <span className="jx-projectOptionName">{t('默认')}</span>
              {!boundProjectId && <img src="/home/check.svg" alt="" className="jx-modeCheckIcon" />}
            </div>
          ),
          onClick: () => unbindChatProject(currentChatId),
        },
        ...projects.map((p) => ({
          key: `proj-${p.project_id}`,
          label: (
            <div className="jx-projectOption">
              {(p.kind as string) === 'local'
                ? <LaptopOutlined className="jx-projectOptionIcon" />
                : <FolderOutlined className="jx-projectOptionIcon" />}
              <span className="jx-projectOptionName" title={p.name}>{p.name}</span>
              {boundProjectId === p.project_id && <img src="/home/check.svg" alt="" className="jx-modeCheckIcon" />}
            </div>
          ),
          onClick: () => onPickProject(p.project_id, p.name),
        })),
      ],
    },
    { type: 'divider' as const },
    // 新建入口严格跟随安装时选择的运行形态：纯本机只显示本地项目，纯云端
    // 只显示云端项目，只有双模式同时显示两者。网页端保持原来的个人项目入口。
    ...(isDesktopShell
      ? [
          ...(canCreateCloudProject
            ? [{
                key: 'proj-new-cloud',
                label: (
                  <div className="jx-projectOption">
                    <FolderAddOutlined className="jx-projectOptionIcon" />
                    <span className="jx-projectOptionName">{t('新建云端项目')}</span>
                  </div>
                ),
                onClick: () => setProjectCreateModalOpen(true),
              }]
            : []),
          ...(canCreateLocalProject
            ? [{
                key: 'proj-new-local',
                label: (
                  <div className="jx-projectOption">
                    <LaptopOutlined className="jx-projectOptionIcon" />
                    <span className="jx-projectOptionName">{t('新建本地项目')}</span>
                  </div>
                ),
                onClick: () => {
                  window.location.href = '/__desktop/pick-local-folder';
                },
              }]
            : []),
        ]
      : [
          {
            key: 'proj-new',
            label: (
              <div className="jx-projectOption">
                <FolderAddOutlined className="jx-projectOptionIcon" />
                <span className="jx-projectOptionName">{t('新建个人项目')}</span>
              </div>
            ),
            onClick: () => setProjectCreateModalOpen(true),
          },
        ]),
  ];
  return (
    <>
      <Dropdown
        trigger={['click']}
        placement="topLeft"
        overlayClassName="jx-projectMenu"
        onOpenChange={(open) => {
          setProjectOpen(open);
          if (open && projects.length === 0) void fetchProjects();
        }}
        menu={{ items: projectMenuItems }}
      >
        <button
          type="button"
          className={`jx-composerChip jx-projectDropBtn${boundProjectId ? ' bound' : ''}${projectOpen ? ' open' : ''}`}
          aria-label={boundProjectId
            ? t('本对话属于项目「{name}」，点击切换', { name: boundProjectName })
            : t('选择项目，当前为默认（不归属项目）')}
          title={t('选择项目')}
        >
          {boundProjectId
            ? <FolderOpenOutlined className="jx-projectDropIcon" />
            : <FolderOutlined className="jx-projectDropIcon" />}
          <span className="jx-projectDropName jx-composerChip-label">{boundProjectId ? boundProjectName : t('默认')}</span>
          <ChipChevron />
        </button>
      </Dropdown>
      <CreateProjectModal
        onCreated={(pid) => {
          const created = useProjectStore.getState().list.find((p) => p.project_id === pid);
          bindChatProject(currentChatId, pid, created?.name || t('项目'));
        }}
      />
    </>
  );
}
