import { apiRequest, isHybridDual, unwrapData, type ActiveChatRunSummary, type RecentRunChat } from './api';
export interface SidebarRunStatus {
  chat_id:string; run_id:string; message_id:string; status:string;
  created_at:string; updated_at:string; target?:'local';
  /** Server read state of this run's result (absent on backends that do not track it). */
  read?:boolean; read_seq?:number; message_seq?:number|null;
}
export async function listSidebarChatRuns(recentSeconds:number) {
  const targets:Array<'local'|undefined>=isHybridDual()?[undefined,'local']:[undefined];
  const results=await Promise.all(targets.map(async target=>{
    const result=await apiRequest<unknown>('/v1/chats/active-runs?include_status=true&recent_seconds='+recentSeconds,undefined,target);
    return {target,data:unwrapData<{items?:ActiveChatRunSummary[];recent_chat_ids?:string[];statuses?:SidebarRunStatus[]}>(result)};
  }));
  const items:ActiveChatRunSummary[]=[],recent:RecentRunChat[]=[],statuses:SidebarRunStatus[]=[];
  for(const {target,data} of results){
    items.push(...(data.items??[]).map(item=>({...item,target})));
    recent.push(...(data.recent_chat_ids??[]).map(chat_id=>({chat_id,target})));
    statuses.push(...(data.statuses??[]).map(item=>({...item,target})));
  }
  return {items,recent,statuses};
}

/** Mark a chat read up to a message on the backend that owns the chat (never moves back). */
export async function markChatRead(chatId:string,messageId:string,target?:'local') {
  const result=await apiRequest<unknown>('/v1/chats/'+encodeURIComponent(chatId)+'/read',
    {method:'POST',body:JSON.stringify({message_id:messageId}),keepalive:true},target);
  return unwrapData<{chat_id:string;read_seq:number}>(result);
}
/** The Personal Agent conversation lives on the cloud server in every mode. */
export async function markPersonalRead(seq:number) {
  const result=await apiRequest<unknown>('/v1/personal-agent/read',
    {method:'POST',body:JSON.stringify({seq}),keepalive:true},'cloud');
  return unwrapData<{read_seq:number;latest_seq:number}>(result);
}
