import { randomUUID } from 'node:crypto';
import { RequestError, type ContentBlock } from '@agentclientprotocol/sdk';
import type {Session} from './sessions.js';
import type {SendOptions} from '../engine/engine.js';

/** Decode only supplied data. Resource URLs never trigger arbitrary network requests. */
export async function promptContent(s:Session, blocks:ContentBlock[], signal:AbortSignal) {
  const parts:string[]=[];
  const files:File[]=[];
  for(const block of blocks) {
    if(block.type==='text') parts.push(block.text);
    else if(block.type==='resource_link') parts.push(`[Resource: ${block.name}] ${block.uri}`);
    else if(block.type==='resource' && 'text' in block.resource) parts.push(`[Resource: ${block.resource.uri}]\n${block.resource.text}`);
    else if(block.type==='image' || (block.type==='resource' && 'blob' in block.resource)) {
      const data=block.type==='image'?block.data:('blob' in block.resource?block.resource.blob:'');
      const mime=block.type==='image'?block.mimeType:block.resource.mimeType ?? 'application/octet-stream';
      if(data.length>14000000 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(data))
        throw RequestError.invalidParams(undefined,'Invalid or oversized base64 content');
      const bytes=Buffer.from(data,'base64');
      if(!bytes.length || bytes.length>10000000 || (block.type==='image' && !mime.startsWith('image/')))
        throw RequestError.invalidParams(undefined,'Invalid attachment');
      const extension:Record<string,string>={'image/png':'png','image/jpeg':'jpg','image/gif':'gif','image/webp':'webp','application/pdf':'pdf','text/plain':'txt'};
      files.push(new File([bytes],`attachment-${randomUUID()}.${extension[mime] ?? 'bin'}`,{type:mime}));
    } else throw RequestError.invalidParams(undefined,`Unsupported prompt content: ${block.type}`);
  }
  const text=parts.join('\n') || (files.length?'Please process the attached files.':'');
  if(!text.trim() || text.length>100000 || files.length>10) throw RequestError.invalidParams(undefined,'Invalid prompt size');
  const attachments:NonNullable<SendOptions['attachments']>=[];
  for(const file of files) {
    if(signal.aborted) throw new DOMException('Cancelled','AbortError');
    const upload=await s.engine.upload(s.id,file,signal);
    attachments.push({file_id:upload.file_id,name:upload.name,mime_type:upload.mime_type});
  }
  return {text,attachments};
}
