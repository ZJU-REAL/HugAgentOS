import { message } from 'antd';
import { getSession } from '../../api';
import { t } from '../../i18n';
import { abilitySlug } from '../../routing/subPages';
import { useCatalogStore } from '../../stores';
import { useChatStore } from '../../stores/chatStore';
import { chatDraftKey, useComposerStore } from '../../stores/composerStore';
import { usePluginStore } from '../../stores/pluginStore';
import { pickSiteEditChat } from '../../utils/history';
import { applicationRequest, type Application, type ApplicationEditor } from './applicationApi';

export async function ensureSitesPluginInstalled(): Promise<boolean> {
  // First ensure the installed-plugin list is up to date (it may have just been installed/uninstalled elsewhere).
  await usePluginStore.getState().fetchInstalled(true).catch(() => {});
  const installed = usePluginStore
    .getState()
    .installed.some((p) => p.slug === 'sites' && p.enabled !== false);
  if (!installed) {
    message.info(t('首次创建站点需要安装插件，请先在能力中心 → 插件里安装后再创建'));
    // 不带类别地进能力中心会落到默认的「智能体」——提示让人去装插件、点过去却是别的页面。
    useCatalogStore.getState().setPanel('ability_center', abilitySlug('plugins'));
    return false;
  }
  return true;
}


/** MCP sources and publication live in the cloud, including desktop dual mode. */
export async function startMcpEdit(app: Application): Promise<void> {
  if (!(await ensureSitesPluginInstalled())) return;
  try {
    const editor = await applicationRequest<ApplicationEditor>(`/v1/applications/${app.id}/editor`,
      { method: 'POST' }, 'cloud');
    const state = useChatStore.getState();
    const existing = editor.chat_id ? state.store.chats[editor.chat_id]
      : pickSiteEditChat(Object.values(state.store.chats).filter(chat => chat.runTarget !== 'local'), editor.project_id);
    if (existing || editor.chat_id) {
      const original = existing || await getSession(editor.chat_id!);
      state.updateStore(store => ({ ...store,
        chats: { ...store.chats, [original.id]: { ...original, siteChat: true, planChat: false, batchChat: false, workflowChat: false,
          planModeActive: false, batchModeActive: false, workflowModeActive: false,
          projectId: editor.project_id, projectName: editor.project_name, runTarget: 'cloud' } },
        order: store.order.includes(original.id) ? store.order : [original.id, ...store.order],
      }));
      state.setCurrentChatId(original.id);
    } else {
      state.enterSiteMode({ projectId: editor.project_id, projectName: editor.project_name,
        title: app.title, resourceKind: 'mcp' });
    }
    const current = useChatStore.getState();
    current.setChatRunTarget(current.currentChatId, undefined);
    const plugin = usePluginStore.getState().installed.find(item => item.slug === 'sites' && item.enabled !== false)!;
    useComposerStore.getState().activate(chatDraftKey(current.currentChatId), {
      activePlugin: { id: plugin.install_id, name: plugin.name },
    });
    useCatalogStore.getState().setPanel('chat');
  } catch (error) {
    message.error(error instanceof Error ? error.message : t('操作失败'));
  }
}
