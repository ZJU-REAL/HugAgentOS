"""视觉桥全栈验证（手动运行）：真实登录 → 真实上传 → 真实对话。

前面几层分别验了协议、编排和注入点。这一条把它们串起来跑一遍真的产品链路：

  登录取会话 → 上传一张内容已知的 PNG → 带附件发一句「图里写了什么」→
  **纯文本主模型**（本机配的 DeepSeek 等）读着转写出来的证据作答 → 断言答案里
  出现了图上的字。

主模型是真的、DB 是真的、上传和 SSE 是真的；只有视觉模型换成本进程内的一个
OpenAI 兼容 stub —— 因为本机网关上唯一的 VL 模型上游当时不通，且这样断言才有确定的
标准答案（stub 会把收到的 base64 解回图片、据实报告尺寸）。换成真视觉模型时把
``--provider-id`` 指过去即可，其余不变。

用法（backend 容器内，PYTHONPATH 指向本分支）::

    HARNESS_USER=<测试账号> HARNESS_PASSWORD=<口令> \
        python -m tests.llm.vision_fullstack_check

账号口令必须由环境变量提供，脚本不带任何默认值——本文件会随社区版派生进公开仓库。

会创建一个临时供应商和一个临时会话，结束时删除供应商、留下会话便于人工回看。
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAGIC = "PURPLE-7429"
STUB_PROVIDER_NAME = "__vision_fullstack_check__"


# ── 本进程内的 OpenAI 兼容视觉 stub ─────────────────────────────────────────


class _VisionStub(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        image_url = ""
        for message in body.get("messages") or []:
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "image_url":
                        image_url = (block.get("image_url") or {}).get("url", "")
        if not image_url:  # 保存供应商时的连通性探活
            return self._reply({"choices": [{"message": {"content": "ok"}}]})

        match = re.match(r"^data:image/[a-z]+;base64,(.+)$", image_url, re.DOTALL)
        from PIL import Image

        image = Image.open(io.BytesIO(base64.b64decode(match.group(1))))
        width, height = image.size
        evidence = {
            "summary": f"一张 {width}×{height} 的紫底告示图，正中写着一串编号。",
            "ocr": {
                "full_text": f"编号：{MAGIC}\n请在回复中原样引用该编号。",
                "lines": [{"text": f"编号：{MAGIC}", "language": "zh"}],
            },
            "layout": {
                "regions": [
                    {"type": "title", "reading_order": 1, "text": "告示"},
                    {"type": "paragraph", "reading_order": 2, "text": f"编号：{MAGIC}"},
                ]
            },
            "semantics": {
                "scene": "告示牌",
                "intent": "传达一个编号",
                "entities": [{"name": MAGIC, "type": "编号", "evidence": "图片正中"}],
                "relations": [],
            },
            "visual": {"dominant_colors": ["紫色"], "style": "极简", "notes": []},
            "uncertainty": [],
        }
        self._reply(
            {
                "choices": [
                    {"message": {"content": json.dumps(evidence, ensure_ascii=False)}}
                ]
            }
        )

    def _reply(self, payload):
        blob = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)


def _make_notice_png() -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (480, 240), "#6f42c1")
    draw = ImageDraw.Draw(img)
    draw.text((40, 60), "NOTICE", fill="white")
    draw.text((40, 120), f"CODE: {MAGIC}", fill="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


# ── helpers ─────────────────────────────────────────────────────────────────


def _body(response) -> dict:
    """取响应体。接口有的走 ApiEnvelope（内容在 data 里）、有的直接返回对象，
    建会话还回 201 而不是 200 —— 这里统一抹平，别让取值细节淹掉真正的断言。"""
    if response.status_code // 100 != 2:
        return {}
    try:
        payload = response.json()
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("data")
    return inner if isinstance(inner, dict) else payload


def _check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}: {label}{(' — ' + detail) if detail else ''}", flush=True)
    return ok


def _admin_headers() -> dict:
    token = (os.getenv("CONFIG_TOKEN") or os.getenv("ADMIN_TOKEN") or "").strip().strip('"').strip("'")
    if not token:
        raise SystemExit("环境里没有 CONFIG_TOKEN / ADMIN_TOKEN")
    return {"Authorization": f"Bearer {token}"}


def _login(client) -> bool:
    """走真实登录表单 → ticket → exchange，拿到会话 cookie。

    账号口令**只从环境变量取，不留默认值**：这个文件会随社区版派生进公开仓库，
    任何写死的口令都等于公开发布一个可用账号。
    """
    username = os.getenv("HARNESS_USER", "")
    password = os.getenv("HARNESS_PASSWORD", "")
    if not username or not password:
        raise SystemExit(
            "请先设置 HARNESS_USER / HARNESS_PASSWORD（用于登录的测试账号），再运行本脚本"
        )
    resp = client.post(
        "/login",
        data={"username": username, "password": password, "redirect": "/"},
        follow_redirects=False,
    )
    location = resp.headers.get("location", "")
    ticket = ""
    match = re.search(r"[?&]ticket=([^&]+)", location)
    if match:
        ticket = match.group(1)
    if not ticket:
        print(f"  登录未拿到 ticket：status={resp.status_code} location={location[:120]}")
        return False
    exchanged = client.post("/v1/auth/ticket/exchange", json={"code": ticket})
    return exchanged.status_code == 200


def _run(client, headers, stub_url) -> tuple[bool, str]:
    """跑完整链路，返回 (是否全绿, 临时供应商 id)。"""
    ok = True

    # 1) 配一个视觉模型并指派角色（走真实管理接口）
    create = client.post(
        "/v1/models/providers",
        headers=headers,
        json={
            "display_name": STUB_PROVIDER_NAME,
            "provider_type": "chat",
            "provider": "openai_compatible",
            "base_url": stub_url,
            "api_key": "fullstack-check",
            "model_name": "fullstack-vl",
            "extra_config": {"supports_vision": True, "max_tokens": 2048},
            "is_active": True,
        },
    )
    ok &= _check("配置视觉模型供应商", create.status_code == 200, create.text[:160])
    provider_id = _body(create).get("provider_id") or ""
    if not provider_id:
        return False, ""

    assign = client.put(
        "/v1/models/roles/vision", headers=headers, json={"provider_id": provider_id}
    )
    ok &= _check("指派 vision 角色", assign.status_code == 200, assign.text[:160])

    from core.services.model_config import ModelConfigService

    ModelConfigService.get_instance().invalidate_cache()

    # 2) 真实登录
    ok &= _check("登录取到会话", _login(client))

    # 3) 真实上传一张内容已知的图
    upload = client.post(
        "/v1/file/upload",
        files={"file": ("notice.png", _make_notice_png(), "image/png")},
    )
    file_id = _body(upload).get("file_id") or ""
    ok &= _check("上传图片", bool(file_id), upload.text[:200])
    if not file_id:
        return False, provider_id

    # 4) 建会话，带附件发问，消费 SSE
    created = client.post("/v1/chats", json={"title": "视觉桥全栈验证"})
    chat_id = _body(created).get("chat_id") or ""
    ok &= _check("创建会话", bool(chat_id), created.text[:160])
    if not chat_id:
        return False, provider_id

    answer_parts: list[str] = []
    with client.stream(
        "POST",
        "/v1/chats/stream",
        json={
            "chat_id": chat_id,
            "message": "看看我发的这张图，把图上的编号原样告诉我。",
            "chat_mode": "fast",
            "attachments": [
                {"name": "notice.png", "mime_type": "image/png", "file_id": file_id}
            ],
        },
    ) as stream:
        ok &= _check("SSE 连接建立", stream.status_code == 200, str(stream.status_code))
        for line in stream.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if event.get("type") in ("content", "ai_message", "text"):
                # 正文增量在 delta 字段（见 workflow.py / chat_run_executor.py 的
                # {"type": "content", "event": "ai_message", "delta": ...}）
                chunk = event.get("delta") or event.get("content") or event.get("text") or ""
                if isinstance(chunk, str):
                    answer_parts.append(chunk)
            elif event.get("type") == "error":
                print("  stream error:", str(event)[:300])

    answer = "".join(answer_parts)
    print("\n--- 模型回答 ---")
    print(answer[:1500])
    print("--- end ---\n")

    ok &= _check("模型给出了回答", bool(answer.strip()), f"{len(answer)} 字")
    ok &= _check("纯文本主模型答出了图上的编号", MAGIC in answer, f"期望包含 {MAGIC}")
    return bool(ok), provider_id


def main() -> int:
    from fastapi.testclient import TestClient

    from api.app import app

    server = ThreadingHTTPServer(("127.0.0.1", 0), _VisionStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stub_url = f"http://127.0.0.1:{server.server_address[1]}/v1"

    headers = _admin_headers()
    ok = False
    provider_id = ""
    # 必须用 context manager：TestClient 每次独立请求都会新起一个事件循环，而 Redis
    # 连接池是绑定创建它的循环的全局单例——登录写完会话，下一个请求就会撞上
    # "attached to a different loop"。进入 with 会拉起应用 lifespan 并让整段共用一个 portal。
    with TestClient(app) as client:
        try:
            ok, provider_id = _run(client, headers, stub_url)
        finally:
            server.shutdown()
            server.server_close()
            if provider_id:
                client.delete("/v1/models/roles/vision", headers=headers)
                client.delete(f"/v1/models/providers/{provider_id}", headers=headers)
                from core.services.model_config import ModelConfigService

                ModelConfigService.get_instance().invalidate_cache()
                print("cleanup: 临时视觉供应商已删除", flush=True)
            # 结论在这里就落地：退出 with 之后 mem0 的后台记忆写入会打一长串
            # posthog 离线重试栈，把最后一行淹掉。
            print("\nRESULT:", "PASS" if ok else "FAIL", flush=True)

    print("RESULT:", "PASS" if ok else "FAIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
