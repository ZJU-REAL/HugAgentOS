/** Connection-owned MCP facade over the negotiated ACP client filesystem and terminal. */
import {createServer} from 'node:http';
import {randomBytes} from 'node:crypto';
import {isAbsolute, relative} from 'node:path';
import {Server} from '@modelcontextprotocol/sdk/server/index.js';
import {StreamableHTTPServerTransport} from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import {CallToolRequestSchema, ListToolsRequestSchema, type Tool} from '@modelcontextprotocol/sdk/types.js';
import type {AgentContext, ClientCapabilities} from '@agentclientprotocol/sdk';
import type {Session} from './sessions.js';
import {abortable} from './permissions.js';

export async function clientTools(s: Session, client: AgentContext, caps: ClientCapabilities) {
  const tools: Tool[]=[];
  const fileSchema={type:'object' as const,properties:{path:{type:'string'}},required:['path']};
  if (caps.fs?.readTextFile) tools.push({name:'client_read_text_file',description:'Read a workspace file through the ACP client',inputSchema:fileSchema,annotations:{readOnlyHint:true}});
  if (caps.fs?.writeTextFile) tools.push({name:'client_write_text_file',description:'Write a workspace file through the ACP client',inputSchema:{...fileSchema,properties:{...fileSchema.properties,content:{type:'string'}},required:['path','content']}});
  if (caps.terminal) tools.push({name:'client_run_command',description:'Execute a command through the ACP client terminal in the session workspace',inputSchema:{type:'object',properties:{command:{type:'string'},args:{type:'array',items:{type:'string'}}},required:['command']}});
  if (!tools.length) return undefined;
  const secret=randomBytes(32).toString('hex');
  const connections=new Set<Server>();
  const terminals=new Set<string>();
  async function cleanup(method:'terminal/kill'|'terminal/release',terminalId:string) {
    const signal=AbortSignal.timeout(2000);
    try {await abortable(client.request(method,{sessionId:s.id,terminalId},{cancellationSignal:signal}),signal);}
    catch {process.stderr.write('ACP terminal cleanup unavailable; client must release its terminal.\n');}
  }
  async function invoke(name:string, args:Record<string,unknown>) {
    const signal=s.active?.controller.signal;
    if (!signal || signal.aborted || s.closing || s.unavailable) throw new Error('No active turn');
    if (!tools.some(t=>t.name===name)) throw new Error('Tool not negotiated');
    if (name==='client_read_text_file' || name==='client_write_text_file') {
      const path=args.path;
      if (typeof path!=='string' || !isAbsolute(path)) throw new Error('Absolute file path required');
      const rel=relative(s.cwd,path);
      if (isAbsolute(rel) || rel==='..' || rel.startsWith('../')) throw new Error('File outside session workspace');
      if (name==='client_read_text_file') return abortable(client.request('fs/read_text_file',{sessionId:s.id,path},{cancellationSignal:signal}),signal);
      if (typeof args.content!=='string' || args.content.length>1000000) throw new Error('Invalid file content');
      return abortable(client.request('fs/write_text_file',{sessionId:s.id,path,content:args.content},{cancellationSignal:signal}),signal);
    }
    if (typeof args.command!=='string' || !args.command || args.command.length>4096) throw new Error('Invalid command');
    const argv=args.args ?? [];
    if (!Array.isArray(argv) || argv.length>128 || argv.some(x=>typeof x!=='string')) throw new Error('Invalid arguments');
    // Keep ownership of late create replies so cancellation cannot leak a client terminal.
    const created=client.request('terminal/create',{sessionId:s.id,command:args.command,args:argv,cwd:s.cwd,outputByteLimit:65536},{cancellationSignal:signal}) as Promise<{terminalId:string}>;
    created.then(async value=>{if(signal.aborted) await cleanup('terminal/release',value.terminalId);}).catch(()=>{});
    const {terminalId}=await abortable(created,signal);
    terminals.add(terminalId);
    try {
      await abortable(client.request('terminal/wait_for_exit',{sessionId:s.id,terminalId},{cancellationSignal:signal}),signal);
      return await abortable(client.request('terminal/output',{sessionId:s.id,terminalId},{cancellationSignal:signal}),signal);
    } finally {
      if (signal.aborted) await cleanup('terminal/kill',terminalId);
      await cleanup('terminal/release',terminalId);
      terminals.delete(terminalId);
    }
  }
  const http=createServer(async(req,res)=>{
    if (req.headers.authorization!=='Bearer '+secret || req.url!=='/mcp') {res.writeHead(403);res.end();return;}
    const server=new Server({name:'hugagent-acp-client',version:'1'},{capabilities:{tools:{}}});
    const transport=new StreamableHTTPServerTransport({sessionIdGenerator:undefined,enableJsonResponse:true});
    server.setRequestHandler(ListToolsRequestSchema,async()=>({tools}));
    server.setRequestHandler(CallToolRequestSchema,async({params})=>{
      try {return {content:[{type:'text',text:JSON.stringify(await invoke(params.name,params.arguments ?? {}))}]};}
      catch {return {isError:true,content:[{type:'text',text:'Client operation unavailable, rejected, or cancelled'}]};}
    });
    connections.add(server);
    res.on('close',()=>{connections.delete(server);void server.close();});
    try {await server.connect(transport);await transport.handleRequest(req,res);}
    catch {if(!res.headersSent) res.writeHead(500);res.end();}
  });
  await new Promise<void>((resolve,reject)=>{http.once('error',reject);http.listen(0,'127.0.0.1',resolve);});
  const address=http.address();
  if (!address || typeof address==='string') throw new Error('Client tool listener unavailable');
  return {config:{transport:'streamable_http',url:`http://127.0.0.1:${address.port}/mcp`,headers:{Authorization:'Bearer '+secret}},
    async close(){
      await Promise.allSettled([...terminals].map(terminalId=>cleanup('terminal/release',terminalId)));
      await Promise.allSettled([...connections].map(c=>c.close()));
      http.closeAllConnections();
      await new Promise<void>(resolve=>http.close(()=>resolve()));
    }};
}
