import assert from 'node:assert/strict';
import { useAuthStore, useChatStore } from '../src/stores';
import { reloadChatHistory, loadOlderMessages, ensureFullMessages } from '../src/hooks/chatHistoryLoader';
const responses: object[]=[];
const urls: string[]=[];
globalThis.fetch=async (url) => {
  urls.push(String(url));
  assert.ok(responses.length,'unexpected network request');
  return new Response(JSON.stringify({code:0,data:responses.shift()}),{status:200,headers:{'content-type':'application/json'}});
};
const page=(first:number,last:number,hasOlder:boolean)=>({
  items:Array.from({length:last-first+1},(_,i)=>({message_id:`m${i+first}`,chat_seq:i+first,role:'assistant',content:`message ${i+first}`,created_at:'2026-10-01T00:00:00Z'})),
  next_before_seq:first,pagination:{has_next:hasOlder},
});
useAuthStore.setState({authUser:{user_id:'u',username:'u'}});
const st=useChatStore.getState();
useChatStore.setState({store:{...st.store,chats:{c:{id:'c',title:'test',messages:[],createdAt:0,updatedAt:0}}}});
responses.push(page(1,30,false));
assert.ok(await reloadChatHistory('c'));
assert.equal(useChatStore.getState().messagePaging.c.beforeSeq,1);
// Forty new remote messages make the refreshed page disjoint.
responses.push(page(41,70,true));
assert.ok(await reloadChatHistory('c'));
assert.equal(useChatStore.getState().messagePaging.c.beforeSeq,41);
assert.equal(useChatStore.getState().messagePaging.c.hasOlder,true);
responses.push(page(11,40,true));
assert.equal(await loadOlderMessages('c'),30);
assert.ok(urls.at(-1)?.includes('before_seq=41'));
// An overlapping refresh must retain the already loaded older cursor.
responses.push(page(41,70,true));
await reloadChatHistory('c');
assert.equal(useChatStore.getState().messagePaging.c.beforeSeq,11);
responses.push(page(1,10,false));
assert.equal(await loadOlderMessages('c'),10);
assert.equal(useChatStore.getState().store.chats.c.messages.length,70);
assert.equal(useChatStore.getState().messagePaging.c.hasOlder,false);
console.log('History cursor: disjoint refresh, overlap, complete pagination passed');

// A page containing only hidden internal messages must not stop full export.
useChatStore.getState().setMessagePaging('c',{nextPage:2,beforeSeq:41,hasOlder:true,loading:false});
responses.push({items:[],next_before_seq:11,pagination:{has_next:true}});
responses.push(page(1,10,false));
await ensureFullMessages('c');
assert.equal(useChatStore.getState().messagePaging.c.hasOlder,false);
console.log('Hidden-only history page advances instead of truncating export');
