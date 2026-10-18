import { Suspense, useEffect } from 'react';
import { useKbTab, useMySpaceTab } from '../../routing/subPages';
import { useMySpaceStore } from '../../stores/mySpaceStore';
import { useEditionStore } from '../../stores';
import { AgentWorkspaceView, usePersonalAgentName } from '../../personalAgentEdition';
import { CatalogPanel } from '../catalog';
import { ShareRecordsPage } from '../share';
import { NotificationList } from './NotificationList';
import { SpaceFavorites } from './SpaceFavorites';
import { SpaceFileBrowser } from './SpaceFileBrowser';
import { motion } from 'motion/react';
import { SPRING } from '../../utils/motionTokens';
import { useCatalogStore, useChatStore } from '../../stores';
import { useSpaceCanvas } from './useSpaceCanvas';
import { MySpaceSearchModal } from './MySpaceSearchModal';
import { TABS } from './mySpaceNavigation';

export function MySpacePanel() {
  const tab = useMySpaceTab();
  const kb = useKbTab();
  const { assetScope, setAssetScope, setTab, syncTab } = useMySpaceStore();
  const edition = useEditionStore(state => state.edition);
  const teams = useEditionStore(state => state.loaded ? !!state.features.multi_tenancy : true);
  const agentName = usePersonalAgentName();
  const kind = teams ? assetScope : 'personal';
  useEffect(() => { if (useMySpaceStore.getState().tab !== tab) syncTab(tab); }, [tab, syncTab]);
  const openFile = useSpaceCanvas();
  const tabs = TABS.filter(item => (item.scope !== 'team' || teams) && (item.kb !== 'public' || edition !== 'ce')
    && (!item.requiresPersonalAgent || !!agentName));
  const active = tabs.find(item => item.tab === tab && (!item.scope || item.scope === kind) && (!item.kb || item.kb === kb));
  return <div className="jx-mySpace"><div className="jx-mySpace-shell">
    <div className="jx-mySpace-header">
      <div className="jx-mySpace-titleBar"><h1 className="jx-mySpace-title">{tab === 'agent' ? agentName : active?.label}</h1></div>
      <div className="jx-mySpace-tabs">
        {tabs.map(item => <button key={item.key}
          className={'jx-mySpace-tab' + (tab === item.tab && (!item.scope || item.scope === kind) && (!item.kb || item.kb === kb) ? ' active' : '')}
          onClick={() => {
            if (item.tab === 'search') { useMySpaceStore.getState().openSearch(); return; }
            if (item.scope) setAssetScope(item.scope);
            setTab(item.tab, item.kb);
          }}>
          <span>{item.tab === 'agent' ? agentName : item.label}</span>
          {item === active && <motion.span layoutId="jx-mySpace-tabInk" className="jx-mySpace-tabInk" transition={SPRING.ink} />}
        </button>)}
      </div>
    </div>
    <div className="jx-mySpace-body">
      {tab === 'assets' ? <SpaceFileBrowser key={kind} kind={kind} />
        : tab === 'favorites' ? <SpaceFavorites />
        : tab === 'kb' ? <CatalogPanel />
        : tab === 'shares' ? <ShareRecordsPage embedded hideEmbeddedDesc />
        : tab === 'notifications' ? <NotificationList />
        : <Suspense fallback={null}><AgentWorkspaceView /></Suspense>}
    </div>
  </div>
    <MySpaceSearchModal onOpenChat={id => { useChatStore.getState().setCurrentChatId(id); useCatalogStore.getState().setPanel('chat'); }} onPreviewFile={openFile} />
  </div>;
}
