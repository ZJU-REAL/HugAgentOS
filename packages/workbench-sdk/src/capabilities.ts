export interface FileReference {
 fileId: string;
 name: string;
 size: number;
 mimeType?: string;
}
export interface CapabilityMethods {
 query<T = unknown>(source: string, params?: Record<string, unknown>, signal?: AbortSignal): Promise<T>;
 pickFile(options?: {accept?: string; source?:'personal'|'device'}, signal?: AbortSignal): Promise<FileReference | null>;
 saveFile(options: {fileId: string; name?: string}, signal?: AbortSignal): Promise<{saved: boolean}>;
 composeChat(options: {text: string}, signal?: AbortSignal): Promise<{composed: boolean}>;
 openApplication(options: {applicationKey: string; path?: string}, signal?: AbortSignal): Promise<{opened: boolean}>;
}
type Request = <T = unknown>(method: string, params?: Record<string, unknown>, signal?: AbortSignal) => Promise<T>;
export function capabilityMethods(request: Request): CapabilityMethods {
 return {
  query: (source, params = {}, signal) => request('data.query', {source, params}, signal),
  pickFile: (options = {}, signal) => request('file.pick', options, signal),
  saveFile: (options, signal) => request('file.save', options, signal),
  composeChat: (options, signal) => request('chat.compose', options, signal),
  openApplication: (options, signal) => request('navigation.open', options, signal),
 };
}
