import type {Connection} from './index.js';
export interface RuntimeCapabilities {
 platform:'web'|'tauri'|'electron'|'unsupported';
 mode:'local'|'cloud'|'dual'|'unsupported';
 openExternal:boolean;pickFile:boolean;saveFile:boolean;local:boolean;cloud:boolean;reason?:string;
}
/** These methods cross the same versioned channel. No native API is exposed. */
export function runtimeCapabilities(connection:Connection,signal?:AbortSignal):Promise<RuntimeCapabilities> {
 return connection.request('platform.runtimeCapabilities',{},signal);
}
export function openExternal(connection:Connection,url:string,signal?:AbortSignal):Promise<void> {
 return connection.request('platform.openExternal',{url},signal);
}
