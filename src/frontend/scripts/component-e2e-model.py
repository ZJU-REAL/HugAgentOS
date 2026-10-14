from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json,time
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  body=json.dumps({'object':'list','data':[{'id':'component-test','object':'model'}]}).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body)
 def do_POST(self):
  req=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))))
  text='端到端测试已完成。附件内容可以在预览中核对。'
  if req.get('stream'):
   self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
   for delta in [{'role':'assistant'},{'reasoning_content':'先核对输入内容。'},{'reasoning_content':'再检查界面和附件状态。'},{'content':text}]:
    frame={'id':'e2e','object':'chat.completion.chunk','created':1,'model':'component-test','choices':[{'index':0,'delta':delta,'finish_reason':None}]}
    self.wfile.write(('data: '+json.dumps(frame,ensure_ascii=False)+'\n\n').encode());self.wfile.flush();time.sleep(.7)
   self.wfile.write(('data: '+json.dumps({'id':'e2e','object':'chat.completion.chunk','created':1,'model':'component-test','choices':[{'index':0,'delta':{},'finish_reason':'stop'}]})+'\n\ndata: [DONE]\n\n').encode())
  else:
   body={'id':'e2e','object':'chat.completion','created':1,'model':'component-test','choices':[{'index':0,'message':{'role':'assistant','content':text},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':20,'total_tokens':30}}
   self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(body,ensure_ascii=False).encode())
ThreadingHTTPServer(('0.0.0.0',8081),Handler).serve_forever()
