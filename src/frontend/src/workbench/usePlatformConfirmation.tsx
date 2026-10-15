import {t} from '../i18n';
import {useCallback,useEffect,useRef,useState} from 'react';
import {Modal} from 'antd';
interface Pending {
 method:string;detail:string;perform:()=>Promise<unknown>;
 resolve:(value:unknown)=>void;reject:(error:Error)=>void;clear:()=>void;
}
/** perform executes directly inside a trusted host button click. */
export function usePlatformConfirmation(){
 const [pending,setPending]=useState<Pending|null>(null);
 const active=useRef<Pending|null>(null);
 const cancel=useCallback(()=>{
  const item=active.current;active.current=null;setPending(null);
  if(item){item.clear();item.reject(new Error('cancelled'));}
 },[]);
 useEffect(()=>cancel,[cancel]);
 const approve=useCallback((method:string,detail:string,perform:()=>Promise<unknown>,signal:AbortSignal)=>new Promise<unknown>((resolve,reject)=>{
  if(signal.aborted){reject(new Error('cancelled'));return;}
  if(active.current){reject(new Error('confirmation_busy'));return;}
  signal.addEventListener('abort',cancel,{once:true});
  const item={method,detail,perform,resolve,reject,clear:()=>signal.removeEventListener('abort',cancel)};
  active.current=item;setPending(item);
 }),[cancel]);
 const allow=(event:React.MouseEvent)=>{
  if(!event.nativeEvent.isTrusted)return;
  const item=active.current;if(!item)return;
  active.current=null;setPending(null);item.clear();
  // Call before awaiting so Web user activation is still available.
  try{void item.perform().then(item.resolve,item.reject);}catch(error){item.reject(error instanceof Error?error:new Error('bridge_error'));}
 };
 return {approve,dialog:pending?<Modal open title={t(pending.method==='file.pick'?'选择设备文件并上传':pending.method==='file.save'?'保存文件':'打开外部链接')} footer={null} onCancel={cancel}>
  <p>{pending.detail}</p><button type="button" onClick={cancel}>{t('取消')}</button>
  <button type="button" onClick={allow}>{t('继续')}</button></Modal>:null};
}
