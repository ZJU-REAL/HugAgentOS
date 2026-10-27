import type {ChatHistoryMessage} from '../engine/engine.js';
import type {SessionUpdate} from '@agentclientprotocol/sdk';
import {Events} from './events.js';

export function historyUpdates(message:ChatHistoryMessage):SessionUpdate[] {
  if(message.role!=='user' && message.role!=='assistant') return [];
  const result:SessionUpdate[]=[];
  const text=(value:unknown)=>{if(typeof value==='string' && value) result.push({sessionUpdate:message.role==='user'?'user_message_chunk':'agent_message_chunk',content:{type:'text',text:value}});};
  const thought=(value:unknown)=>{if(typeof value==='string' && value) result.push({sessionUpdate:'agent_thought_chunk',content:{type:'text',text:value}});};
  const thoughts=Array.isArray(message.thinking)?message.thinking:typeof message.thinking==='string'?[message.thinking]:[];
  const events=new Events();
  const tools=Array.isArray(message.tool_calls)?message.tool_calls:[];
  const shown=new Set<number>();
  const tool=(index:number)=>{
    const value=tools[index];
    if(!value || typeof value!=='object' || shown.has(index)) return;
    shown.add(index);result.push(...events.convert({...value,type:'tool_result'}));
  };
  const meta=message.metadata && typeof message.metadata==='object'?message.metadata as Record<string,unknown>:{};
  if(Array.isArray(meta.segments) && meta.segments.length) {
    for(const segment of meta.segments) {
      if(segment.type==='thinking' && Number.isInteger(segment.index)) {const value=thoughts[segment.index];thought(typeof value==='string'?value:value?.text);}
      if(segment.type==='text') text(segment.text);
      if(segment.type==='tool' && Number.isInteger(segment.index)) tool(segment.index);
    }
    tools.forEach((_,index)=>tool(index));
  } else {thoughts.forEach(value=>thought(typeof value==='string'?value:value?.text));tools.forEach((_,index)=>tool(index));text(message.content);}
  if(Array.isArray(meta.attachments)) for(const file of meta.attachments) {
    if(file && typeof file.name==='string' && typeof file.file_id==='string')
      result.push({sessionUpdate:message.role==='user'?'user_message_chunk':'agent_message_chunk',content:{type:'resource_link',name:file.name,uri:'hugagent-file:'+encodeURIComponent(file.file_id)}});
  }
  if(Array.isArray(meta.artifacts)) for(const file of meta.artifacts) {
    const uri=file?.download_url ?? file?.url;
    if(typeof uri==='string' && typeof file?.name==='string') result.push({sessionUpdate:'agent_message_chunk',content:{type:'resource_link',name:file.name,uri}});
  }
  return result;
}
