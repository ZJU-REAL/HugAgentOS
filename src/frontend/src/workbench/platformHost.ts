import {createBrowserPlatform} from './browserPlatform';
import {createNativePlatform} from './nativePlatform';
import {projectRuntimeCapabilities,type PlatformAdapter} from './platformAdapter';
import {useDeploymentModeStore} from '../stores/deploymentModeStore';
import {useAuthStore} from '../stores/authStore';
/** One adapter per account/service lifetime. Frames never import this module. */
export function createHostPlatform():PlatformAdapter {
 const state=()=>useDeploymentModeStore.getState();
 const boot=window.top===window.self?window.__HG_PLATFORM__:undefined;
 const kind=boot && ['tauri','electron'].includes(boot.platform)?boot.platform:
  state().isDesktop?'unsupported':'web';
 const capabilities=()=>projectRuntimeCapabilities(kind,state().provisionMode,
  state().provisionMode==='dual'?state().localReady && state().localService?.ready===true:
   state().localService?.ready===true);
 const adapter=kind==='tauri'||kind==='electron'
  ?createNativePlatform(boot!,capabilities):createBrowserPlatform(capabilities);
 const previousUser=useAuthStore.getState().authUser?.user_id;
 const previousServer=state().serverBase;
 let closed=false;
 const close=()=>{if(closed)return;closed=true;offAuth();offMode();adapter.dispose();};
 const offAuth=useAuthStore.subscribe(next=>{if(next.authUser?.user_id!==previousUser || next.loggingOut || next.authExpiredUrl)close();});
 const offMode=useDeploymentModeStore.subscribe(next=>{if(next.serverBase!==previousServer)close();});
 return {...adapter,
  async openExternal(url,signal,revalidate){if(!capabilities().openExternal)throw new Error('host_incompatible');return adapter.openExternal(url,signal,revalidate);},
  async pickFile(signal){if(!capabilities().pickFile)throw new Error('host_incompatible');return adapter.pickFile(signal);},
  async saveFile(name,content,signal){if(!capabilities().saveFile)throw new Error('host_incompatible');return adapter.saveFile(name,content,signal);},
  dispose:close};
}
