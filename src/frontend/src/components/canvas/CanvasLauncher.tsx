import { useEffect, useRef, useState } from 'react';
import { AppstoreOutlined, FileOutlined, GlobalOutlined, MessageOutlined, ArrowRightOutlined } from '@ant-design/icons';
import { apiRequest, unwrapData, uploadFile } from '../../api';
import type { ModuleContribution } from '../../plugin-ui/types';
import type { ResourceBinding } from '../../plugin-ui/module/resource';
import { t } from '../../i18n';
import { resolveText } from '../../plugin-ui/i18n';
import { useCanvasStore } from '../../stores/canvasStore';
import { usePluginUiStore } from '../../stores/pluginUiStore';
import { useChatStore } from '../../stores/chatStore';
import { useCatalogStore } from '../../stores/catalogStore';
import { composerActions, useComposerStore } from '../../stores/composerStore';
import { CanvasTabBar } from './CanvasTabBar';
import { tabTitle, tabIcon } from './canvasTabPresentation';
import { useCanvasLauncherStore } from './canvasLauncherStore';
import { useCanvasGuard } from './useCanvasGuard';
import './CanvasLauncher.css';

export function CanvasLauncher() {
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const fileInput = useRef<HTMLInputElement>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  const tabs = useCanvasStore(s => s.tabs);
  const activeTabId = useCanvasStore(s => s.activeTabId);
  const items = usePluginUiStore(s => s.items);
  const navigation = useCanvasLauncherStore(s => s.navigation);
  const dismiss = useCanvasLauncherStore(s => s.dismiss);
  const guard = useCanvasGuard();
  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busy) dismiss();
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [busy, dismiss]);
  const navigator = navigation?.tabId === activeTabId ? navigation : null;
  const activate = (id: string) => {
    if (id === activeTabId) { dismiss(); return; }
    guard(() => { dismiss(); useCanvasStore.getState().activateTab(id); });
  };
  const openPanel = (panel: 'ability_center' | 'chat') => guard(() => {
    dismiss();
    useCanvasStore.getState().closeCanvas();
    useCatalogStore.getState().setPanel(panel);
  });
  const openFile = async (file: File) => {
    setBusy(true); setError('');
    const { currentChatId, currentUserId } = useChatStore.getState();
    try {
      const uploaded = await uploadFile(file, currentChatId || undefined);
      const current = useChatStore.getState();
      if (!mounted.current || current.currentUserId !== currentUserId || current.currentChatId !== currentChatId) return;
      dismiss();
      useCanvasStore.getState().openCanvas({
        file_id: uploaded.file_id, name: uploaded.name || file.name,
        url: uploaded.download_url, mime_type: uploaded.mime_type, size: uploaded.size,
        origin: uploaded.origin, chat_id: currentChatId || undefined,
      });
    } catch (cause) { if (mounted.current) setError(cause instanceof Error ? cause.message : t('上传失败')); }
    finally { if (mounted.current) setBusy(false); }
  };
  const navigate = async () => {
    if (!navigator || !url.trim() || busy) return;
    setBusy(true); setError('');
    try {
      await navigator.navigate(url.trim());
      if (mounted.current) dismiss();
    } catch (cause) { if (mounted.current) setError(cause instanceof Error ? cause.message : t('打开失败')); }
    finally { if (mounted.current) setBusy(false); }
  };
  const openModule = async (slug: string, module: ModuleContribution) => {
    const canvas = useCanvasStore.getState();
    const chat = useChatStore.getState();
    const chatId = canvas.pluginTarget?.chatId || canvas.artifact?.chat_id || chat.currentChatId;
    const existing = tabs.find(tab => tab.kind === 'plugin' && tab.target.slug === slug
      && tab.target.canvasId === module.id && tab.target.status === 'success'
      && tab.target.resource?.status !== 'closed' && (!chatId || tab.target.chatId === chatId));
    if (existing) { activate(existing.id); return; }
    if (!chatId) { setError(t('请先打开对话')); return; }
    guard(() => {
      setBusy(true); setError('');
      const target = chat.store.chats[chatId]?.runTarget;
      void apiRequest('/v1/plugins/' + encodeURIComponent(slug) + '/resources/' + encodeURIComponent(module.id),
        { method: 'POST', body: JSON.stringify({ chat_id: chatId }) }, target || undefined)
        .then(response => {
          const current = useChatStore.getState();
          if (!mounted.current || current.currentUserId !== chat.currentUserId || current.currentChatId !== chat.currentChatId) return;
          const output = unwrapData<{ resource: ResourceBinding }>(response);
          dismiss();
          useCanvasStore.getState().openPluginView({
            slug, canvasId: module.id, chatId, title: resolveText(module.title),
            status: 'success', output,
          });
        }).catch(cause => { if (mounted.current) setError(cause instanceof Error ? cause.message : t('打开失败')); })
        .finally(() => { if (mounted.current) setBusy(false); });
    });
  };
  const modules = items.flatMap(item => (item.contributes.modules || [])
    .filter(module => module.surface === 'canvas' && !!module.resource_binding)
    .map(module => ({ slug: item.slug, module })));
  const shortcuts = items.flatMap(item => (item.contributes.shortcuts || [])
    .filter(entry => !!entry.prompt).map(entry => ({ ...entry, key: item.slug + ':' + entry.id })));
  return <aside className="jx-rightSidebar jx-canvasLauncher" aria-label={t('新标签页')}>
    <CanvasTabBar />
    {navigator && <form className="jx-canvasLauncher-address" onSubmit={event => { event.preventDefault(); void navigate(); }}>
      <GlobalOutlined aria-hidden />
      <input aria-label={t('网页地址')} placeholder={t('搜索 Bing 或输入网页地址')} value={url}
        disabled={busy} onChange={event => setUrl(event.target.value)}
        onKeyDown={event => { if (event.key === 'Enter' && event.nativeEvent.isComposing) event.preventDefault(); }} />
      <button type="submit" disabled={busy || !url.trim()} aria-label={t('打开网页')}><ArrowRightOutlined aria-hidden /></button>
    </form>}
    <div className="jx-canvasLauncher-content">
      {error && <p role="alert">{error}</p>}
      <h2>{t('工具')}</h2>
      <div className="jx-canvasLauncher-tools">
        <button disabled={busy} onClick={() => guard(() => fileInput.current?.click())}><FileOutlined aria-hidden /><span>{t('文件')}</span></button>
        <button disabled={busy} onClick={() => openPanel('chat')}><MessageOutlined aria-hidden /><span>{t('返回对话')}</span></button>
        <button disabled={busy} onClick={() => openPanel('ability_center')}><AppstoreOutlined aria-hidden /><span>{t('能力中心')}</span></button>
        {modules.map(({ slug, module }) => <button key={slug + ':' + module.id} disabled={busy} onClick={() => void openModule(slug, module)}>
          {module.icon ? <img src={module.icon} alt="" /> : <GlobalOutlined aria-hidden />}<span>{resolveText(module.title)}</span>
        </button>)}
      </div>
      {shortcuts.length > 0 && <>
        <h2>{t('插件快捷功能')}</h2>
        <div className="jx-canvasLauncher-tools">{shortcuts.map(entry => <button key={entry.key} disabled={busy} onClick={() => guard(() => {
          const store = useComposerStore.getState();
          const owner = store.scopes[store.activeKey];
          if (!owner) { setError(t('请先打开对话')); return; }
          composerActions(owner).setInput(resolveText(entry.prompt));
          dismiss(); useCanvasStore.getState().closeCanvas(); useCatalogStore.getState().setPanel('chat');
        })}>{entry.icon ? <img src={entry.icon} alt="" /> : <AppstoreOutlined aria-hidden />}<span>{resolveText(entry.label)}</span></button>)}</div>
      </>}
      <h2>{t('已打开内容')}</h2>
      <div className="jx-canvasLauncher-suggested">{tabs.map(tab => <button key={tab.id} disabled={busy} onClick={() => activate(tab.id)}>
        <span>{tabIcon(tab)}</span><span title={tabTitle(tab)}>{tabTitle(tab)}</span>
      </button>)}</div>
      <input ref={fileInput} type="file" hidden onChange={event => {
        const file = event.target.files?.[0]; event.target.value = '';
        if (file) void openFile(file);
      }} />
      {busy && <p role="status">{t('正在处理…')}</p>}
    </div>
  </aside>;
}
