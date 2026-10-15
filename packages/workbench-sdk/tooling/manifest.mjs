import {readFileSync,realpathSync,statSync} from 'node:fs';
import {resolve,relative,isAbsolute} from 'node:path';
import {validateApplication,validateContributions,HOST_CAPABILITIES} from '@hugagent/workbench-protocol';
export const PREVIEW_CAPABILITIES=[...new Set([...HOST_CAPABILITIES,'workspace.navigate','file.pick'])];
export function readManifest(path,{assets,capabilities=PREVIEW_CAPABILITIES}={}) {
 const reject=(code,path='/',message=code)=>({valid:false,issues:[{code,path,message}]});
 let input;
 try {
  if(statSync(path).size>256*1024)return reject('manifest_too_large');
  input=JSON.parse(readFileSync(path,'utf8'));
 } catch {return reject('invalid_json');}
 if(input?.schemaVersion) {
  const issues=validateApplication(input,{capabilities});
  return {valid:!issues.length,issues,application:input};
 }
 const ui=input?.extensions?.['org.hugagent']?.ui;
 if(ui?.version!==3)return reject('ui_version','/extensions/org.hugagent/ui/version','Workspace requires UI v3');
 const result=validateContributions(ui.contributes,{source:'plugin',capabilities});
 const issues=[...result.errors];
 if(!result.applications.length && !issues.length)issues.push({code:'missing_application',path:'/applications',message:'Declare a workspace application'});
 if(assets) {
  for(const [index,module] of result.modules.entries()) {
   try {readAsset(assets,module.entry);}
   catch {issues.push({code:'invalid_asset',path:'/modules/'+index+'/entry',message:'Missing, escaping or oversized asset'});}
  }
 }
 return {valid:!issues.length,issues,manifest:input,contributions:result};
}
export function readAsset(root,name) {
 if(typeof name!=='string'||!/^web\/(?:[A-Za-z0-9_-]+\/)*[A-Za-z0-9_.-]+\.html$/.test(name))throw Error('invalid_asset');
 const base=realpathSync(root),path=realpathSync(resolve(base,name)),rel=relative(base,path);
 if(rel.startsWith('..')||isAbsolute(rel)||!statSync(path).isFile()||statSync(path).size>8*1024*1024)throw Error('invalid_asset');
 return readFileSync(path,'utf8');
}
