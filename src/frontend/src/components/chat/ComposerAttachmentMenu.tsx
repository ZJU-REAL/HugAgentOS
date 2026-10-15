import { Dropdown } from 'antd';
import { FileImageOutlined, FileTextOutlined, CloudDownloadOutlined, AppstoreOutlined,
  RobotOutlined, OrderedListOutlined, ApiOutlined, SyncOutlined, PartitionOutlined, LinkOutlined } from '@ant-design/icons';
import { MySpaceImportModal } from '../file';
import { AgentIcon } from '../agent/AgentIcon';
import { SkillAvatar } from '../catalog/skillIcons';
import { McpIcon } from '../catalog/McpIcon';
import { PluginAvatar } from '../catalog/PluginIconPicker';
import { IconPlus } from '../common/DshIcons';
import { t } from '../../i18n';
import type { InputAreaProps, ComposerOptions } from './composerTypes';
import type { ComposerState } from './useComposerState';
import type { ComposerEditor } from './useComposerEditor';

export function ComposerAttachmentMenu({ state, editor, options, fileInputRef }: {
  state: ComposerState; editor: ComposerEditor; options: ComposerOptions;
  fileInputRef: InputAreaProps['fileInputRef'];
}) {
  const { disableMention } = options;
  const { isModeOn, isAppAllowed, onEnterMode, canReadImage, imageInputRef, showLoopEntry,
    loopMode, setLoopMode, agents, skills, connectors, installedPlugins, activeLocalMode,
    setMySpaceImportOpen, setAttachOpen, fetchAgents, attachOpen, mySpaceImportOpen,
  } = state;
  const { onPickAgentFromMenu, onPickSkillFromMenu, onPickConnectorFromMenu, onPickPluginFromMenu } = editor;
  const planActive = isModeOn('plan');
  const workflowActive = isModeOn('workflow');
  const activeSuffix = t('（已开启）');
  const modeItems = [
    ...(isAppAllowed('plan_mode') ? [{
      key: 'mode-plan',
      icon: <OrderedListOutlined />,
      label: planActive ? t('计划模式{suffix}', { suffix: activeSuffix }) : t('计划模式'),
      onClick: () => onEnterMode('plan'),
    }] : []),
    // 工作流模式：面对成百上千个同构工作项时，让智能体写一段作业脚本交后台并发跑。
    // 与计划模式一样是**用户显式触发**的模式——不进入就不注册 run_job、
    // 不注入批量提示词，普通问答完全不受影响。
    {
      key: 'mode-workflow',
      icon: <PartitionOutlined />,
      label: workflowActive ? t('工作流模式{suffix}', { suffix: activeSuffix }) : t('工作流模式'),
      onClick: () => onEnterMode('workflow'),
    },
  ];
  const items = [
    {
      key: 'image',
      icon: <FileImageOutlined />,
      // 没有任何模型能读图时把话说在前面：图还是能传（当附件留档、换模型后仍可用），
      // 但别让用户以为这一轮模型看得见。
      label: canReadImage ? t('上传图片') : t('上传图片（当前模型不识图）'),
      onClick: () => imageInputRef.current?.click(),
    },
    { key: 'file', icon: <FileTextOutlined />, label: t('上传文件'), onClick: () => fileInputRef.current?.click() },
    { type: 'divider' as const },
    ...modeItems,
    ...(showLoopEntry ? [{
      key: 'mode-loop',
      icon: <SyncOutlined />,
      label: loopMode ? t('自主循环{suffix}', { suffix: activeSuffix }) : t('自主循环'),
      onClick: () => setLoopMode(true),
    }] : []),
    ...((modeItems.length > 0 || showLoopEntry) ? [{ type: 'divider' as const }] : []),
    ...(!disableMention ? [{
      key: 'agents',
      popupClassName: 'jx-capabilityMenu',
      icon: <RobotOutlined />,
      label: t('@智能体'),
      children: (() => {
        const callable = agents || [];
        if (callable.length === 0) {
          return [{ key: 'agents-empty', label: t('暂无可用智能体'), disabled: true }];
        }
        return callable.map((a) => ({
          key: `agent-${a.agent_id}`,
          icon: <span className="jx-capabilityMenuIcon"><AgentIcon agent={a} size={20} /></span>,
          label: a.name,
          onClick: () => onPickAgentFromMenu(a),
        }));
      })(),
    }] : []),
    {
      key: 'skills',
      popupClassName: 'jx-capabilityMenu',
      icon: <AppstoreOutlined />,
      label: t('技能'),
      children: (() => {
        const callable = skills || [];
        if (callable.length === 0) {
          return [{ key: 'skills-empty', label: t('暂无可用技能'), disabled: true }];
        }
        return callable.map((s) => ({
          key: `skill-${s.id}`,
          icon: <span className="jx-capabilityMenuIcon"><SkillAvatar icon={s.icon} name={s.name} seed={s.id} size={20} round /></span>,
          label: s.name,
          onClick: () => onPickSkillFromMenu(s.id, s.name),
        }));
      })(),
    },
    {
      key: 'connectors',
      popupClassName: 'jx-capabilityMenu',
      icon: <LinkOutlined />,
      label: t('连接器'),
      children: (() => {
        const callable = connectors || [];
        if (callable.length === 0) {
          return [{ key: 'connectors-empty', label: t('暂无可用连接器'), disabled: true }];
        }
        return callable.map((c) => ({
          key: `connector-${c.id}`,
          icon: <span className="jx-capabilityMenuIcon"><McpIcon id={c.id} icon={c.icon} size={20} /></span>,
          label: c.name,
          onClick: () => onPickConnectorFromMenu(c.id, c.name),
        }));
      })(),
    },
    {
      key: 'plugins',
      popupClassName: 'jx-capabilityMenu',
      icon: <ApiOutlined />,
      label: t('插件'),
      children: (() => {
        const callable = installedPlugins.filter((p) => p.callable !== false);
        if (callable.length === 0) {
          return [{ key: 'plugins-empty', label: t('暂无已安装插件'), disabled: true }];
        }
        return callable.map((p) => ({
          key: `plugin-${p.install_id}`,
          icon: <span className="jx-capabilityMenuIcon"><PluginAvatar icon={p.icon} size={20} round /></span>,
          label: p.name,
          onClick: () => onPickPluginFromMenu(p),
        }));
      })(),
    },
    ...(activeLocalMode
      ? []
      : [
          { type: 'divider' as const },
          {
            key: 'myspace',
            icon: <CloudDownloadOutlined />,
            label: t('从我的空间导入'),
            onClick: () => setMySpaceImportOpen(true),
          },
        ]),
  ];
  return (
    <>
      <Dropdown
        trigger={['click']}
        placement="topRight"
        overlayClassName="jx-attachMenu"
        onOpenChange={(open) => {
          setAttachOpen(open);
          if (!open) return;
          if (!disableMention && agents.length === 0) void fetchAgents();
        }}
        menu={{ items }}
      >
        <button
          type="button"
          className={`jx-attachBtn${attachOpen ? ' open' : ''}`}
          title={t('添加文件')}
          aria-label={t('添加文件')}
        >
          <IconPlus size={16} className="jx-attachIcon" />
        </button>
      </Dropdown>
      <MySpaceImportModal draftKey={state.draftKey} open={mySpaceImportOpen} onClose={() => setMySpaceImportOpen(false)} />

    </>
  );
}
