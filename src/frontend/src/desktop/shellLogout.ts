/** Tauri custom commands are deliberately unregistered. The native endpoint
 * checks exact Origin, same-origin Fetch Metadata, and the host-only secret. */
export async function logoutViaHostPlatform():Promise<boolean|null> {
 if(typeof window==='undefined' || window.top!==window.self ||
    window.__HG_PLATFORM__?.platform!=='tauri')return null;
 const response=await fetch('/__desktop/workbench/platform',{
  method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Native':window.__HG_PLATFORM__.key},
  body:JSON.stringify({op:'logout'}),signal:AbortSignal.timeout(10000),
 });
 if(!response.ok)throw new Error('desktop_logout_failed');
 const value=await response.json() as {cleared?:boolean};
 if(value.cleared!==true)throw new Error('desktop_logout_failed');
 return true;
}
