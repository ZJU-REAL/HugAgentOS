import type {Connection} from './index';
export interface ApplicationSnapshot {
 revision:number; schemaVersion:string|null; payload:Record<string,unknown>|null; resetRequired:boolean;
}
/** UI snapshots only. Never include tokens, cookies, passwords or file contents.
 * A resetRequired result must be shown to the user before reset:true is sent.
 * Save does not clear dirty: the application clears it after its business save.
 */
export function applicationState(connection:Connection){
 return {
  load:(signal?:AbortSignal)=>connection.request<ApplicationSnapshot>('state.load',{},signal),
  save:(value:{revision:number;schemaVersion:string;payload:Record<string,unknown>;reset?:boolean},signal?:AbortSignal)=>
   connection.request<ApplicationSnapshot>('state.save',value,signal),
  setDirty:(dirty:boolean,signal?:AbortSignal)=>connection.request<{dirty:boolean}>('lifecycle.setDirty',{dirty},signal),
 };
}
