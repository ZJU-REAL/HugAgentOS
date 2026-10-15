import { useCallback } from 'react';
import { useTaskNotificationStore } from '../stores/taskNotificationStore';
import type { ChatMessage } from '../types';
import { useReadAtBottom } from './useReadAtBottom';
export function useChatResultRead(chatId:string,messages:ChatMessage[],element:HTMLElement|null,hidden:boolean) {
  const notice=useTaskNotificationStore(s=>s.notices['chat:'+chatId]);
  const ready=!!notice&&!notice.seen&&['completed','failed'].includes(notice.status)&&
    messages.some(m=>m.messageId===notice.messageId&&!m.isStreaming);
  const acknowledge=useCallback(()=>{
    if(notice)useTaskNotificationStore.getState().markSeen(notice.key,notice.id);
  },[notice]);
  useReadAtBottom(element,ready&&!hidden,acknowledge);
}
