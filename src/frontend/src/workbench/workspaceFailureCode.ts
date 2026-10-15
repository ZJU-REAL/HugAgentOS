/** Only host-generated handshake outcomes may request a diagnostic receipt. */
export function workspaceFailureCode(value:unknown):'bridge_timeout'|'page_failed'|undefined {
 if(value==='bridge_timeout')return 'bridge_timeout';
 if(value==='page_failed'||value==='host_incompatible'||value==='invalid_message')return 'page_failed';
 return undefined;
}
