/** Opt-in real image upload/vision verification using an installed CLI. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {Readable,Writable} from 'node:stream';
import {readFileSync,writeFileSync} from 'node:fs';
import {client,ndJsonStream} from '@agentclientprotocol/sdk';
const {ACP_IMAGE_WORKSPACE:cwd,ACP_IMAGE_FIXTURE:fixture,ACP_IMAGE_REPORT:reportPath}=process.env;
if(!cwd?.includes('/.scratch/') || !fixture || !reportPath) throw new Error('Explicit fixture/workspace/report required');
const child=spawn('hugagent',['--mode','local','--output','acp'],{cwd,stdio:['pipe','pipe','pipe']});child.stderr.resume();
let text='';
let approvals=0;const peer=client({name:'image-acceptance'}).onRequest('session/request_permission',({params})=>{approvals++;return {outcome:{outcome:'selected',optionId:params.options.find(x=>x.kind==='allow_once').optionId}};}).onNotification('session/update',({params})=>{if(params.update.sessionUpdate==='agent_message_chunk')text+=params.update.content.text??'';});
const connection=peer.connect(ndJsonStream(Writable.toWeb(child.stdin),Readable.toWeb(child.stdout)));
const agent=connection.agent;const report={};
try {
 await agent.request('initialize',{protocolVersion:1,clientCapabilities:{}});
 const s=await agent.request('session/new',{cwd,mcpServers:[]},{timeout:180000});
 report.sessionId=s.sessionId;
 const answer=await agent.request('session/prompt',{sessionId:s.sessionId,prompt:[{type:'text',text:'请看这张图片，只回答图片主体是什么颜色。'},{type:'image',mimeType:'image/png',data:readFileSync(fixture).toString('base64')}]},{timeout:180000});
 assert.equal(answer.stopReason,'end_turn');assert.match(text,/红|red/i);report.ok=true;
 await agent.request('session/close',{sessionId:s.sessionId});
} catch(error){report.ok=false;report.error=String(error);report.errorCode=error.code;process.exitCode=1;}
finally {report.approvals=approvals;report.text=text;connection.close();child.kill();writeFileSync(reportPath,JSON.stringify(report,null,2));console.log(JSON.stringify(report));}
