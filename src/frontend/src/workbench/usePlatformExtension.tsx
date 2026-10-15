import {useEffect,useMemo} from 'react';
import {createHostPlatform} from './platformHost';
import {createPlatformExtension} from './platformExtension';
import {usePlatformConfirmation} from './usePlatformConfirmation';
/** Called only for an opened instance. authorize is WB-09's current binding check. */
export function usePlatformExtension(authorize:(method:string,signal:AbortSignal)=>Promise<void>){
 const adapter=useMemo(()=>createHostPlatform(),[]);
 const {approve,dialog}=usePlatformConfirmation();
 const extension=useMemo(()=>createPlatformExtension(adapter,{authorize,approve}),[adapter,authorize,approve]);
 useEffect(()=>()=>adapter.dispose(),[adapter]);
 return {adapter,extension,dialog,approve};
}
