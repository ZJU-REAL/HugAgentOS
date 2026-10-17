"""Deterministic model fixture; no external service or credential is used."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import time

REPLY = "端到端测试已完成。附件内容可以在预览中核对。"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"object": "list", "data": [{"id": "component-test", "object": "model"}]}).encode())

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        messages = request.get("messages", [])
        users = [i for i, item in enumerate(messages) if item.get("role") == "user"]
        latest = users[-1] if users else -1
        content = str(messages[latest].get("content", "")) if latest >= 0 else ""
        answered = any(item.get("role") == "tool" for item in messages[latest + 1:])
        question = "[CUSTOMIZATION_QUESTION]" in content and not answered
        available = {tool.get("function", {}).get("name") for tool in request.get("tools", [])}
        if question and "ask_user_question" not in available:
            deltas = [{"content": "Question tool was not registered"}]
            finish = "stop"
        elif question:
            arguments = {"questions": [{"id": "acceptance", "question": "Continue the customer acceptance test?",
                                      "options": [{"label": "Continue"}, {"label": "Stop"}]}]}
            deltas = [{"tool_calls": [{"index": 0, "id": "question-acceptance", "type": "function",
                       "function": {"name": "ask_user_question", "arguments": json.dumps(arguments)}}]}]
            finish = "tool_calls"
        else:
            deltas = [{"role": "assistant"}, {"reasoning_content": "先核对输入内容。"}, {"content": REPLY}]
            finish = "stop"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if request.get("stream") else "application/json")
        self.end_headers()
        if not request.get("stream"):
            self.wfile.write(json.dumps({"id": "e2e", "object": "chat.completion", "model": "component-test",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": REPLY}, "finish_reason": "stop"}]}).encode())
            return
        try:
            for delta in [*deltas, {}]:
                frame = {"id": "e2e", "object": "chat.completion.chunk", "created": 1, "model": "component-test",
                         "choices": [{"index": 0, "delta": delta, "finish_reason": finish if not delta else None}]}
                self.wfile.write(("data: " + json.dumps(frame, ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()
                time.sleep(0.4)
            self.wfile.write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


ThreadingHTTPServer(("0.0.0.0", 8081), Handler).serve_forever()
