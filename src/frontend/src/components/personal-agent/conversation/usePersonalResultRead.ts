import { useCallback } from 'react';
import { useReadAtBottom } from '../../../hooks/useReadAtBottom';
import { useTaskNotificationStore } from '../../../stores/taskNotificationStore';
import type { PersonalMessage } from '../api';
export function usePersonalResultRead(element:HTMLElement|null,messages:PersonalMessage[]){
  const notices=useTaskNotificationStore(s=>s.notices);
  const final=messages.filter(m=>m.role==='assistant'&&!m.streaming);
  const firstStreaming=Math.min(Infinity,...messages.filter(m=>m.streaming).map(m=>m.seq));
  const seq=Math.max(0,...final.filter(m=>m.seq<firstStreaming).map(m=>m.seq));
  const acknowledge=useCallback(()=>{
    useTaskNotificationStore.getState().readPersonal(seq);
  },[seq]);
  // Re-evaluate when a late journal verdict arrives after its final message rendered.
  const callback=useCallback(()=>{void notices;acknowledge();},[notices,acknowledge]);
  useReadAtBottom(element,seq>0,callback);
}
