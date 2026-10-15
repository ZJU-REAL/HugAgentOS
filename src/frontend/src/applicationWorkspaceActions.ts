import {configureWorkspaceActions} from './workbench/workspaceActions';
import {apiRequest} from './api';
import {useComposerStore,readComposer} from './stores/composerStore';
import {navigateTo,pathForPanel} from './routing/navigation';
import {useCatalogStore} from './stores/catalogStore';
import {LAYOUT_ITEMS} from './components/sidebar/items';
import type {CatalogItem} from './workbench/catalog';
import type {PanelKey} from './types';
configureWorkspaceActions({
 compose(text,signal){
  if(signal.aborted)throw new Error('cancelled');
  const composer=useComposerStore.getState(),key=composer.activeKey;
  if(!key||!composer.scopes[key])throw new Error('draft_unavailable');
  readComposer(key).setInput(text);
  useCatalogStore.getState().setPanel('chat');
 },
 async navigate(key,path,signal){
  if(path && (!path.startsWith('/')||path.startsWith('//')||(/[\\?#]/.test(path)||Array.from(path).some(c=>c.charCodeAt(0)<32))
    ||path.split('/').some(p=>p==='.'||p==='..'||/%(?:2e|2f|5c)/i.test(p))))throw new Error('invalid_route');
  const result=await apiRequest<{data:CatalogItem}>('/v1/workbench/applications/'+encodeURIComponent(key),{signal});
  const item=result.data;
  if(!item.availability.available)throw new Error('app_disabled');
  if(signal.aborted)throw new Error('cancelled');
  const target=item.declaration.target;
  if(target.kind==='builtin'){
   const id=key.slice(8);
   const panel=id==='chat'?'chat':LAYOUT_ITEMS[id]?.targetPanel;
   if(!panel)throw new Error('navigation_unavailable');
   useCatalogStore.getState().setPanel(panel as PanelKey);
   navigateTo(pathForPanel(panel as PanelKey,...path.split('/').filter(Boolean)));
  }else if(target.kind==='plugin'){
   navigateTo('/apps/'+encodeURIComponent(key)+path);
  }else throw new Error('navigation_unavailable');
 },
});
