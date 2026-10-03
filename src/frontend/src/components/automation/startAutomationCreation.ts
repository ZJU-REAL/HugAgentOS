import { chatDraftKey, useComposerStore } from '../../stores/composerStore';
import { message } from 'antd';
import { createSession } from '../../api';
import { t } from '../../i18n';
import { navigateTo, pathForAutomationChat } from '../../routing/navigation';
import { abilitySlug } from '../../routing/subPages';
import { useCatalogStore } from '../../stores/catalogStore';
import { useChatStore } from '../../stores/chatStore';
import { usePluginStore } from '../../stores/pluginStore';
import { useProjectStore } from '../../stores/projectStore';
import { AUTOMATION_CHAT_TEMPLATE, resolveAutomationPluginReference } from '../../utils/automationConversation';
/** Persist a setup conversation under Scheduled Tasks and reference its creation plugin. */
export async function startAutomationCreationInChat() {
  await usePluginStore.getState().fetchInstalled(true).catch(() => { });
  const plugin = resolveAutomationPluginReference(usePluginStore.getState().installed);

  if (!plugin) {
    message.info(t('首次通过对话创建定时任务需要安装插件，请先在能力中心 → 插件里安装后再创建'));
    useCatalogStore.getState().setPanel('ability_center', abilitySlug('plugins'));
    return;
  }

  const chat = useChatStore.getState();
  const project = useProjectStore.getState().currentProject;
  const session = await createSession({ title: t('新建定时任务'), metadata: { automation_task_id: 'new' } });
  chat.updateStore(prev => ({ chats: { ...prev.chats, [session.id]: { ...session, automationTaskId: 'new', ...(project ? { projectId: project.project_id, projectName: project.name } : {}) } }, order: [session.id, ...prev.order.filter(id => id !== session.id)] }));
  if (project) chat.bindChatProject(session.id, project.project_id, project.name);
  chat.addBackendSessionId(session.id);
  chat.addLoadedMsgId(session.id);
  useComposerStore.getState().activate(chatDraftKey(session.id), {
    input: AUTOMATION_CHAT_TEMPLATE, activePlugin: plugin,
  });
  chat.adoptChatFromUrl(session.id);
  navigateTo(pathForAutomationChat('new', session.id));
}
