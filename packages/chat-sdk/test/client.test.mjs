import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { ChatClient, ChatApiError, readSSE, sseData } from '../dist/index.js';

test('SSE preserves multiline data, UTF-8, CRLF and releases the reader', async () => {
  const bytes = new TextEncoder().encode(': heartbeat\r\n\r\ndata: {"type":"content",\r\ndata: "delta":"你好"}\r\n\r\ndata: [DONE]\r\n\r\n');
  const response = new Response(new ReadableStream({start(c){ for(const byte of bytes)c.enqueue(Uint8Array.of(byte));c.close(); }}));
  const events=[]; for await(const e of readSSE(response))events.push(e);
  assert.deepEqual(events,[{type:'content',delta:'你好'}]);
  assert.equal(response.body.locked,false);
  assert.equal(sseData('event: message\nid: 2\ndata: x\ndata: y'),'x\ny');
});
test('truncated stream fails explicitly and abort cancels a blocked reader', async () => {
  await assert.rejects(async()=>{for await(const e of readSSE(new Response('data: {"type":"content","delta":"partial"}\n\n')))void e;}, /disconnected/);
  const ctrl=new AbortController();let cancelled=false;
  const response=new Response(new ReadableStream({cancel(){cancelled=true;}}));
  const pending=(async()=>{for await(const e of readSSE(response,ctrl.signal))void e;})();
  ctrl.abort();await assert.rejects(pending, e=>e.name==='AbortError');
  assert.equal(cancelled,true);assert.equal(response.body.locked,false);
});
test('client authenticates, encodes IDs, unwraps errors, uploads and follows events over HTTP', async () => {
 const server=createServer(async(req,res)=>{
   const chunks=[];for await(const chunk of req)chunks.push(chunk);
   const body=Buffer.concat(chunks).toString();
   res.setHeader('Content-Type','application/json');
   if(req.headers.authorization!=='Bearer test') {res.writeHead(401);return res.end('{"detail":"unauthorized"}');}
   if(req.url==='/api/v1/chats' && req.method==='POST')return res.end('{"code":10001,"data":{"chat_id":"a/b","title":"Demo"}}');
   if(req.url==='/api/v1/chats/a%2Fb/messages?page=1&page_size=50')return res.end('{"code":10000,"data":{"items":[{"message_id":"m1","content":"saved"}]}}');
   if(req.url==='/api/v1/file/upload'){assert.match(req.headers['content-type'],/multipart/);assert.match(body,/hello.txt/);return res.end('{"file_id":"f1","name":"hello.txt","size":5,"mime_type":"text/plain","download_url":"/files/f1"}');}
   if(req.url==='/api/v1/agents/responses'){assert.equal(JSON.parse(body).stream,true);res.setHeader('Content-Type','text/event-stream');return res.end('data: {"type":"run_started","run_id":"r1"}\n\ndata: {"type":"content","delta":"reply"}\n\ndata: [DONE]\n\n');}
   if(req.url==='/api/v1/chat-runs/r1/cancel')return res.end('{"code":10000,"data":{"ok":true}}');
   res.writeHead(403);res.end('{"code":40300,"message":"Forbidden","detail":{"code":"access_denied"}}');
 });
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 try{
   const client=new ChatClient({baseUrl:'http://127.0.0.1:'+server.address().port+'/api/',headers:()=>({Authorization:'Bearer test'})});
   assert.equal((await client.createChat({title:'Demo'})).chat_id,'a/b');
   assert.equal((await client.messages('a/b')).items[0].content,'saved');
   assert.equal((await client.upload(new File(['hello'],'hello.txt'))).file_id,'f1');
   const events=[];for await(const e of client.send({chat_id:'a/b',message:'hi'}))events.push(e);
   assert.equal(events[1].delta,'reply');await client.cancel('r1');
   await assert.rejects(client.messages('forbidden'),e=>e instanceof ChatApiError && e.status===403 && e.code===40300);
 }finally{await new Promise(resolve=>server.close(resolve));}
});

test('a terminal native snapshot completes without an artificial disconnect',async()=>{
 const events=[];
 for await(const event of readSSE(new Response('data: {"type":"run_snapshot","state":{"terminal":true}}\n\n')))events.push(event);
 assert.equal(events[0].state.terminal,true);
});
