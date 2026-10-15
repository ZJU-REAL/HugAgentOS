import { apiRequest, isHybridDual, unwrapData, type ActiveChatRunSummary, type RecentRunChat } from './api';
export interface SidebarRunStatus {
  chat_id:string; run_id:string; message_id:string; status:string;
  created_at:string; updated_at:string; target?:'local';
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
