/** Explicit opt-in: exercise the installed CLI against a fresh task workspace. */
import assert from 'node:assert/strict';
import {spawn,spawnSync} from 'node:child_process';
import {Readable,Writable} from 'node:stream';
import {readFileSync,writeFileSync,mkdirSync,existsSync} from 'node:fs';
import {resolve} from 'node:path';
import {client,ndJsonStream} from '@agentclientprotocol/sdk';
const workspace=process.env.ACP_INSTALLED_WORKSPACE;
if(!workspace || !workspace.includes('/.scratch/')) throw new Error('Explicit scratch workspace required');
if(existsSync(workspace)) throw new Error('Use a fresh workspace');
mkdirSync(workspace,{recursive:true});
writeFileSync(workspace+'/orders.py',`def summarize_orders(rows):
    total = 0.0
    for row in rows:
        total += float(row["unit_price"])
    return {"total": str(total), "count": len(rows)}
`);
writeFileSync(workspace+'/test_orders.py',`import unittest
from orders import summarize_orders
class OrdersTest(unittest.TestCase):
    def test_quantity_and_cancelled(self):
        rows = [{"status":"paid","unit_price":"10.20","quantity":3}, {"status":"cancelled","unit_price":"100","quantity":1}]
        self.assertEqual(summarize_orders(rows), {"total":"30.60","count":1})
if __name__ == "__main__": unittest.main()
`);
const python=resolve('../../.venv/bin/python');
const before=spawnSync(python,['-m','unittest','-v'],{cwd:workspace,encoding:'utf8'});
assert.notEqual(before.status,0);
const child=spawn('hugagent',['--mode','local','--output','acp'],{cwd:workspace,stdio:['pipe','pipe','pipe']});
let stderr='';child.stderr.on('data',b=>{stderr+=b;});
const updates=[];let approvals=0;
const peer=client({name:'installed-cli-real-task'})
 .onNotification('session/update',({params})=>updates.push(params))
 .onRequest('session/request_permission',({params})=>{approvals++;return {outcome:{outcome:'selected',optionId:params.options.find(x=>x.kind==='allow_once').optionId}};});
const connection=peer.connect(ndJsonStream(Writable.toWeb(child.stdin),Readable.toWeb(child.stdout)));
const report={workspace,beforeExit:before.status};
try {
 const agent=connection.agent;
 await agent.request('initialize',{protocolVersion:1,clientCapabilities:{}});
 const s=await agent.request('session/new',{cwd:workspace,mcpServers:[]},{timeout:180000});report.sessionId=s.sessionId;
 const result=await agent.request('session/prompt',{sessionId:s.sessionId,prompt:[{type:'text',text:`请修复当前目录 orders.py 中的订单汇总错误。业务规则：只统计 status 为 paid 的订单；金额为单价乘数量，用 Decimal 避免浮点误差；total 返回保留两位小数的字符串，count 返回有效 paid 订单的行数；空输入返回 0.00 和 0；paid 订单的 quantity 必须为正整数（拒绝 bool），单价必须为有限的非负十进制数，不合法时抛 ValueError。取消订单应直接跳过，不校验其金额。请读现有代码，实际修改，补充测试并运行，最后简述结果。请仅处理当前工作目录。Python 测试解释器可使用 ${python}。`}]},{timeout:300000});
 report.stopReason=result.stopReason;assert.equal(result.stopReason,'end_turn');
 const verification=`import sys,unittest
sys.path.insert(0,${JSON.stringify(workspace)})
from orders import summarize_orders
class Independent(unittest.TestCase):
 def test_empty(self):self.assertEqual(summarize_orders([]),{'total':'0.00','count':0})
 def test_paid(self):self.assertEqual(summarize_orders([{'status':'paid','unit_price':'0.10','quantity':3},{'status':'paid','unit_price':'0.20','quantity':2}]),{'total':'0.70','count':2})
 def test_skip(self):self.assertEqual(summarize_orders([{'status':'cancelled','unit_price':'garbage','quantity':0}]),{'total':'0.00','count':0})
 def test_quantity(self):
  for q in [0,-1,1.5,True,'2']:
   with self.assertRaises(ValueError):summarize_orders([{'status':'paid','unit_price':'1','quantity':q}])
 def test_price(self):
  for price in ['-1','NaN','Infinity','invalid']:
   with self.assertRaises(ValueError):summarize_orders([{'status':'paid','unit_price':price,'quantity':1}])
 def test_large(self):self.assertEqual(summarize_orders([{'status':'paid','unit_price':'9999999999999.99','quantity':3}]),{'total':'29999999999999.97','count':1})
unittest.main()
`;
 const checked=spawnSync(python,['-c',verification],{cwd:workspace,encoding:'utf8'});
 report.independentTests={exit:checked.status,output:checked.stdout+checked.stderr};assert.equal(checked.status,0);
 report.toolCalls=updates.filter(x=>x.update.sessionUpdate==='tool_call').map(x=>x.update.title);
 report.finalText=updates.filter(x=>x.update.sessionUpdate==='agent_message_chunk').map(x=>x.update.content.text??'').join('');
 report.approvals=approvals;report.source=readFileSync(workspace+'/orders.py','utf8');report.ok=true;
 await agent.request('session/close',{sessionId:s.sessionId});
} catch(error){report.ok=false;report.error=String(error);report.stderr=stderr.slice(-2000);process.exitCode=1;}
finally {report.commandResults=updates.filter(x=>x.update.sessionUpdate==='tool_call_update' && x.update.title==='Bash').map(x=>({status:x.update.status,output:x.update.rawOutput}));report.toolCalls=updates.filter(x=>x.update.sessionUpdate==='tool_call').map(x=>x.update.title);report.finalText=updates.filter(x=>x.update.sessionUpdate==='agent_message_chunk').map(x=>x.update.content.text??'').join('');report.approvals=approvals;connection.close();child.kill();writeFileSync(process.env.ACP_INSTALLED_REPORT,JSON.stringify(report,null,2));console.log(JSON.stringify(report));}
