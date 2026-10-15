import {getLang} from '../i18n';
import {useEffect,useState} from 'react';
import {Alert,Button,Checkbox,Space,Spin} from 'antd';
import {readPluginUpgrade,decidePluginUpgrade,type PluginUpgrade} from './pluginUpgradeApi';
const messages = {"Upgrade status could not be loaded.": "无法读取升级状态。", "Upgrade decision failed. Reload the status and try again.": "升级操作失败。请刷新状态后重试。", "Loading upgrade status": "正在读取升级状态", "Retry": "重试", "Upgrade declined; the previous version remains installed.": "已拒绝升级；继续保留旧版本。", "Plugin upgrade approval": "插件升级审批", "Plugin upgrade needs approval": "插件升级需要审批", "The previous version and its enablement choices remain active until you approve.": "批准前继续使用旧版本，并保留原有启停选择。", "Start with fresh state in the new version.": "新版本使用全新状态。", "Approve upgrade": "批准升级", "Keep previous version": "保留旧版本"};
const label=(value:keyof typeof messages)=>getLang()==='en'?value:messages[value];
export function PluginUpgradeCard({installId,token,onChanged}:{
 installId:string;token?:string;onChanged?:()=>void|Promise<void>;
}) {
 const [value,setValue]=useState<PluginUpgrade|null>(null);
 const [loading,setLoading]=useState(true),[busy,setBusy]=useState(false);
 const [error,setError]=useState(''),[reset,setReset]=useState(false),[reload,setReload]=useState(0);
 useEffect(()=>{
  const controller=new AbortController();
  setLoading(true);setError('');setReset(false);setValue(null);
  readPluginUpgrade(installId,token,controller.signal).then(data=>{
   if(!controller.signal.aborted)setValue(data);
  }).catch(()=>{
   if(!controller.signal.aborted)setError(label('Upgrade status could not be loaded.'));
  }).finally(()=>{if(!controller.signal.aborted)setLoading(false);});
  return ()=>controller.abort();
 },[installId,token,reload]);
 async function decide(decision:'approve'|'reject') {
  if(!value||busy)return;
  setBusy(true);setError('');
  try {
   await decidePluginUpgrade(value,decision,reset,token);
   await onChanged?.();
   setReload(v=>v+1);
  } catch {setError(label('Upgrade decision failed. Reload the status and try again.'));}
  finally {setBusy(false);}
 }
 if(loading)return <Spin aria-label={label('Loading upgrade status')} size="small"/>;
 if(error)return <Alert type="error" showIcon message={error}
  action={<Button onClick={()=>setReload(v=>v+1)} disabled={busy}>{label('Retry')}</Button>}/>;
 if(!value||value.status==='current')return null;
 if(value.status==='rejected')return <Alert type="info" message={label('Upgrade declined; the previous version remains installed.')}/>;
 return <section aria-label={label('Plugin upgrade approval')} style={{margin:'12px 0'}}>
  <Alert type="warning" showIcon message={label('Plugin upgrade needs approval')}
   description={label('The previous version and its enablement choices remain active until you approve.')}/>
  {value.addedPermissions.length>0&&<ul>{value.addedPermissions.map(p=><li key={p}>{p}</li>)}</ul>}
  {value.stateSchemaChanges.length>0&&<div>
   <ul>{value.stateSchemaChanges.map(s=><li key={s.applicationId}>
    {s.applicationId}: {s.previous} &rarr; {s.next}</li>)}</ul>
   <Checkbox checked={reset} onChange={e=>setReset(e.target.checked)}>
    {label('Start with fresh state in the new version.')}
   </Checkbox>
  </div>}
  <Space style={{marginTop:8}}>
   <Button type="primary" loading={busy}
    disabled={value.stateSchemaChanges.length>0&&!reset} onClick={()=>void decide('approve')}>{label('Approve upgrade')}</Button>
   <Button disabled={busy} onClick={()=>void decide('reject')}>{label('Keep previous version')}</Button>
  </Space>
 </section>;
}
