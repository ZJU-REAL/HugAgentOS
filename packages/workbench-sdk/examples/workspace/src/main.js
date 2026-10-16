import {connect} from '@hugagent/workbench-sdk';
const status=document.querySelector('#status'),result=document.querySelector('#result');
const route=document.querySelector('#route'),save=document.querySelector('#save');
let connection,selected,abort,epoch=0,operation=0;
function render(info) {
 document.body.dataset.theme=info.theme;
 document.documentElement.lang=info.locale;
 route.textContent='Current view: '+(info.route?.path||'overview');
}
async function run(method,params={}) {
 const current=connection,version=epoch,call=++operation;
 if(!current){status.textContent='Not connected. Retry connection.';return;}
 status.textContent='Loading';
 try {
  const data=await current.request(method,params,method==='data.query'?abort?.signal:undefined);
  if(version!==epoch||call!==operation)return;
  result.textContent=JSON.stringify(data,null,2);status.textContent='Ready';
  return data;
 } catch(error) {if(version===epoch&&call===operation){status.textContent=error.message;result.textContent='';}}
}
async function initialize() {
 const version=++epoch;abort?.abort();connection?.dispose();connection=undefined;selected=undefined;save.disabled=true;
 status.textContent='Connecting';result.textContent='';
 try {
  const current=await connect({requiredCapabilities:['host.info','workspace.navigate']});
  if(version!==epoch){current.dispose();return;}
  connection=current;render(current.info);
  current.onUpdate(render);status.textContent='Ready';
 } catch(error) {if(version===epoch)status.textContent=error.message;}
}
for(const view of ['overview','files']) {
 document.querySelector('#'+view).onclick=()=>run('workspace.navigate',{path:'/'+view,replace:false,requestId:'example:'+crypto.getRandomValues(new Uint32Array(1))[0]});
}
document.querySelector('#query').onclick=()=>{
 abort?.abort();abort=new AbortController();void run('data.query',{source:'sample',params:{city:'Beijing'}});
};
document.querySelector('#cancel').onclick=()=>abort?.abort();
document.querySelector('#pick').onclick=async()=>{
 selected=await run('file.pick',{accept:'text/plain'});save.disabled=!selected;
};
save.onclick=()=>selected&&run('file.save',{fileId:selected.fileId,name:selected.name});
document.querySelector('#retry').onclick=initialize;
window.addEventListener('pagehide',()=>{epoch++;abort?.abort();connection?.dispose();});
void initialize();
