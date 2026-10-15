/** Host-only adapters. Never include the native bootstrap in bridge context. */
export const PLATFORM_FILE_LIMIT = 8 * 1024 * 1024;
export interface RuntimeCapabilities {
 platform: 'web'|'tauri'|'electron'|'unsupported';
 mode: 'local'|'cloud'|'dual'|'unsupported';
 openExternal: boolean; pickFile: boolean; saveFile: boolean;
 local: boolean; cloud: boolean; reason?: string;
}
export interface PlatformFile { handle: string; name: string; size: number }
export interface PlatformAdapter {
 runtimeCapabilities(): RuntimeCapabilities;
 openExternal(url: string, signal?: AbortSignal, revalidate?:()=>Promise<void>): Promise<void>;
 pickFile(signal?: AbortSignal): Promise<PlatformFile|null>;
 readFile(handle: string, signal?: AbortSignal): Promise<Blob>;
 saveFile(name: string, content: Blob, signal?: AbortSignal): Promise<boolean>;
 releaseFile(handle: string): void;
 reset(): void;
 dispose(): void;
}
export interface PlatformBootstrap { platform: 'tauri'|'electron'; key: string }
declare global { interface Window { __HG_PLATFORM__?: PlatformBootstrap } }
export function safePlatformUrl(raw: string): string {
 let url: URL;
 try { url = new URL(raw); } catch { throw new Error('invalid_url'); }
 if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.href.length > 4096)
  throw new Error('invalid_url');
 return url.href;
}
export function safeFileName(name: string): string {
 if (!name.trim() || name.length > 200 || (/[\\/:]/.test(name) || Array.from(name).some(c=>c.charCodeAt(0)<32)) || name === '.' || name === '..')
  throw new Error('invalid_params');
 return name;
}
export function projectRuntimeCapabilities(platform: RuntimeCapabilities['platform'], mode: string,
 localReady: boolean): RuntimeCapabilities {
 const actualMode = platform === 'web' ? 'cloud' : mode === 'local_only' ? 'local'
  : mode === 'cloud_only' ? 'cloud' : mode === 'dual' ? 'dual' : 'unsupported';
 const supported = platform !== 'unsupported' && actualMode !== 'unsupported';
 return {platform, mode: actualMode, openExternal: supported, pickFile: supported, saveFile: supported,
  local: supported && actualMode !== 'cloud' && localReady,
  cloud: supported && actualMode !== 'local',
  ...(!supported ? {reason:'host_incompatible'} : actualMode !== 'cloud' && !localReady ? {reason:'origin_unavailable'} : {})};
}
export function assertActive(signal?: AbortSignal): void {
 if (signal?.aborted) throw new Error('cancelled');
}
export async function encodeBlob(blob: Blob): Promise<string> {
 if(blob.size > PLATFORM_FILE_LIMIT) throw new Error('file_too_large');
 const bytes=new Uint8Array(await blob.arrayBuffer());
 let value='';for(let i=0;i<bytes.length;i+=32768)value+=String.fromCharCode(...bytes.subarray(i,i+32768));
 return btoa(value);
}
