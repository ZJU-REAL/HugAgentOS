import {useCallback,useEffect,useRef,useState} from 'react';
import {Modal} from 'antd';
import {t} from '../i18n';
import type {HostConfirm,HostPrompt} from './capabilityHost';
interface Pending {prompt:HostPrompt;resolve:(id:string)=>void;reject:(error:Error)=>void;clear:()=>void}
const TITLES={approval:'批准插件能力',file:'选择供插件使用的文件',save:'保存插件文件',compose:'填写聊天草稿',navigate:'打开另一个应用'} as const;
export function useWorkspaceConfirmation(){
 const [pending,setPending]=useState<Pending|null>(null);
 const [selection,setSelection]=useState('');
 const active=useRef<Pending|null>(null);
 const cancel=useCallback(()=>{const p=active.current;active.current=null;setPending(null);if(p){p.clear();p.reject(new Error('cancelled'));}},[]);
 useEffect(()=>cancel,[cancel]);
 const confirm:HostConfirm=useCallback((prompt,signal)=>new Promise<string>((resolve,reject)=>{
  if(signal.aborted){reject(new Error('cancelled'));return;}
  if(active.current){reject(new Error('confirmation_busy'));return;}
  const onAbort=()=>cancel();
  const value={prompt,resolve,reject,clear:()=>signal.removeEventListener('abort',onAbort)};
  signal.addEventListener('abort',onAbort,{once:true});active.current=value;setSelection('');setPending(value);
 }),[cancel]);
 const allow=(event:React.MouseEvent)=>{
  if(!event.nativeEvent.isTrusted)return;
  const p=active.current;if(!p)return;
  if(p.prompt.kind==='file'&&!selection)return;
  active.current=null;p.clear();setPending(null);p.resolve(selection);
 };
 const dialog=pending?<Modal open title={t(TITLES[pending.prompt.kind])} footer={null} onCancel={cancel}>
  <p>{pending.prompt.detail}</p>
  {pending.prompt.files && <label>{t('文件')}<select value={selection} onChange={e=>setSelection(e.target.value)}>
   <option value="">{t('请选择文件')}</option>
   {pending.prompt.files.map(f=><option key={f.fileId} value={f.fileId}>{f.name}</option>)}
  </select></label>}
  <button type="button" onClick={cancel}>{t('取消')}</button>
  <button type="button" onClick={allow} disabled={pending.prompt.kind==='file'&&!selection}>{t('允许')}</button>
 </Modal>:null;
 return {confirm,dialog};
}
