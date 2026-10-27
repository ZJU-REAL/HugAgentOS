// Scripted Engine at the TUI public boundary. No model, credentials, or product service.
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
export class FakeEngine {
  calls=[]; closed=false; connected=false; mode='ask'; next=0; queued=[]; followups=[];
  compactResult={compacted:true,tokensBefore:48210,tokensAfter:6120};
  constructor(mode='local',{speed=0,cwd='/demo'}={}) {
    this.speed=speed;
    this.info={mode,label:mode==='local'?`local · ${cwd}`:'cloud · agent.example.com',server:'https://agent.example.com',workspace:mode==='local'?cwd:undefined,canAccessCwd:mode==='local'};
  }
  record(method,...args){this.calls.push({method,args});}
  async connect(){this.record('connect');this.connected=true;}
  async close(){this.record('close');this.closed=true;this.wake?.();}
  async newSession(){this.record('newSession');return {chatId:'chat-'+(++this.next),title:'New'};}
  async listSessions(){this.record('listSessions');return [{chatId:'chat-demo',title:'Safari compatibility'}];}
  async history(id){this.record('history',id);return [{message_id:'m',role:'assistant',content:'Safari 15 之前不支持',thinking:'先看看登录组件和构建配置',tool_calls:[{tool_id:'t',tool_name:'read_file',tool_args:{path:'src/pages/Login.tsx'},result:Array(41).fill('line').join('\n'),status:'success'}]}];}
  async listModels(){return [{providerId:'qwen',modelName:'qwen',label:'Qwen',current:true},{providerId:'deepseek',modelName:'deepseek-reasoner',label:'DeepSeek Reasoner'}];}
  async getPermissionMode(){return this.mode;}
  async setPermissionMode(mode){this.mode=mode;}
  async compact(id){this.record('compact',id);await sleep(this.speed*500);return this.compactResult;}
  async activeRun(){return null;}
  async cancel(id){this.record('cancel',id);this.cancelled=true;this.wake?.();}
  async confirm(...args){this.record('confirm',...args);this.decision=args[2];this.wake?.();}
  async answer(...args){this.record('answer',...args);this.wake?.();}
  async steer(...args){this.record('steer',...args);if(args[2]==='followUp')this.followups.push(args[1]);else this.queued.push(args[1]);}
  async *resume(id,offset,signal){this.record('resume',id,offset);yield {type:'run_started',run_id:id};yield {type:'content',delta:'排队回答'};yield {type:'end'};}
  async *send(id,message,options={}){
    this.record('send',id,message,options.model);this.cancelled=false;
    const run='run-'+(++this.next);
    yield {type:'run_started',run_id:run};
    if(message.includes('long')){
      yield {type:'content',delta:'长回答'};
      await sleep(this.speed?250:0);
      for(const text of this.queued.splice(0))yield {type:'steer_applied',message:text};
      for(const text of this.followups.splice(0))yield {type:'queued_run_started',run_id:'followup',message:text,delivery_mode:'followUp'};
    } else if(message.includes('question')){
      const waiting=new Promise(r=>this.wake=r);
      yield {type:'user_question',request_id:'question-1',questions:[
        {id:'db',question:'新服务用哪个数据库',options:[{id:'pg',label:'PostgreSQL',recommended:true}]},
        {id:'features',question:'需要哪些附加功能',multi_select:true,options:[{id:'auth',label:'登录鉴权'}]}]};
      await waiting;this.wake=undefined;
      if(!this.cancelled)yield {type:'content',delta:'已回答：PostgreSQL | 登录鉴权'};
    } else {
      yield {type:'tool_call',tool_id:'ls',tool_name:'list_dir',tool_args:{path:'.'}};
      yield {type:'tool_result',tool_id:'ls',tool_name:'list_dir',result:'a\nb\nc\nd'};
      yield {type:'tool_call',tool_id:'sh',tool_name:'bash',tool_args:{command:'npm test'}};
      yield {type:'tool_result',tool_id:'sh',tool_name:'bash',status:'error',error:'exit code 1'};
      yield {type:'tool_call',tool_id:'wr',tool_name:'write_file',tool_args:{path:'NOTES.md'}};
      const waiting=new Promise(r=>this.wake=r);
      yield {type:'file_confirm',confirm_id:'confirm-1',op:'write',path:'NOTES.md'};
      await waiting;this.wake=undefined;
      yield {type:'tool_result',tool_id:'wr',tool_name:'write_file',status:this.decision==='deny'?'error':'success',error:this.decision==='deny'?'用户拒绝了写入':undefined,result:'saved'};
      yield {type:'content',delta:'项目是一个 TypeScript 包'};
      yield {type:'context_usage',used_tokens:190,context_window:1000};
    }
    yield {type:'end'};
  }
}
