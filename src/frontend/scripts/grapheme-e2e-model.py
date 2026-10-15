"""Deterministic OpenAI-compatible model for the isolated streaming E2E."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import time

CHUNKS = [
    "你好", "👨‍👩‍👧‍👦", "，这是一段流式回复。",
    "\n\n**结论**：", "正文逐字淡入。",
    "\n\n- Markdown结构保持。", "\n- 复制选区保持。",
    "\n\n```js\n", "const value = 42;\n", "```",
    "\n\n验证结束。",
]

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.reply({"object": "list", "data": [{"id": "grapheme-test", "object": "model"}]})

    def reply(self, body):
        encoded = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if not request.get("stream"):
            self.reply({"id": "e2e", "object": "chat.completion", "created": 1,
                        "model": "grapheme-test", "choices": [{"index": 0,
                        "message": {"role": "assistant", "content": "".join(CHUNKS)},
                        "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 60, "total_tokens": 70}})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for content in CHUNKS:
                frame = {"id": "e2e", "object": "chat.completion.chunk", "created": 1,
                         "model": "grapheme-test", "choices": [{"index": 0,
                         "delta": {"content": content}, "finish_reason": None}]}
                self.wfile.write(("data: " + json.dumps(frame, ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()
                time.sleep(0.4)
            self.wfile.write(b'data: {"id":"e2e","object":"chat.completion.chunk","created":1,"model":"grapheme-test","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        except (BrokenPipeError, ConnectionResetError):
            pass

ThreadingHTTPServer(("0.0.0.0", 8081), Handler).serve_forever()
