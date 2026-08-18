"""模型管理侧的真实链路验证（手动运行）。

打真实的 DB 和真实的 ``/v1/models/*`` 路由，走一遍配置多模态模型的完整动作：

  列角色 → 新建带 supports_vision 的供应商 → 指派给 vision 角色 →
  能力接口应答 can_read_image → 视觉桥自己也认为可用 → 清理干净

用法（在 backend 容器内、PYTHONPATH 指向本分支）::

    python -m tests.llm.vision_role_live_check

会创建一个名为 ``__vision_live_check__`` 的临时供应商并在结束时删除；中途失败也会
在 finally 里清理，不会给环境留脏数据。
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

STUB_NAME = "__vision_live_check__"


class _StubHandler(BaseHTTPRequestHandler):
    """一个最小的 OpenAI 兼容端点。

    既要能应答新建供应商时的连通性探活（一句 "hi"，无图），也要能应答真正的识图
    请求（带 image_url），后者会把收到的 base64 解回图片再据实作答——这样这条 live
    check 才既覆盖「配得上」也覆盖「调得通」。
    """

    def log_message(self, *args):
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        content = body.get("messages", [{}])[-1].get("content")
        image_url = ""
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image_url":
                    image_url = (block.get("image_url") or {}).get("url", "")
        if not image_url:  # 连通性探活
            return self._reply({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

        match = re.match(r"^data:image/[a-z]+;base64,(.+)$", image_url, re.DOTALL)
        from PIL import Image

        image = Image.open(io.BytesIO(base64.b64decode(match.group(1))))
        width, height = image.size
        evidence = {
            "summary": f"一张 {width}x{height} 的图。",
            "ocr": {"full_text": f"LIVE-CHECK size={width}x{height}", "lines": []},
            "layout": {"regions": [{"type": "image", "reading_order": 1, "text": "整幅图"}]},
            "semantics": {"scene": "测试图", "entities": [], "relations": []},
            "visual": {"dominant_colors": [], "notes": []},
            "uncertainty": [],
        }
        self._reply(
            {
                "choices": [
                    {"message": {"role": "assistant", "content": json.dumps(evidence, ensure_ascii=False)}}
                ]
            }
        )

    def _reply(self, payload: dict) -> None:
        blob = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)


def _start_stub() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1"


def _sample_png(width: int = 200, height: int = 120) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), "white")
    ImageDraw.Draw(img).text((10, 50), "LIVE-CHECK", fill="black")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def _client():
    from fastapi.testclient import TestClient

    from api.app import app

    return TestClient(app)


def _admin_headers() -> dict:
    token = os.getenv("CONFIG_TOKEN") or os.getenv("ADMIN_TOKEN") or ""
    token = token.strip().strip('"').strip("'")
    if not token:
        raise SystemExit("环境里没有 CONFIG_TOKEN / ADMIN_TOKEN，无法调管理接口")
    return {"Authorization": f"Bearer {token}"}


def _check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"{'PASS' if condition else 'FAIL'}: {label}{(' — ' + detail) if detail else ''}")
    return condition


def main() -> int:
    client = _client()
    headers = _admin_headers()
    ok = True
    provider_id = ""
    server, stub_url = _start_stub()

    try:
        # 1) 角色注册表里有 vision
        resp = client.get("/v1/models/roles", headers=headers)
        roles = {r["role_key"]: r for r in (resp.json().get("data") or [])}
        ok &= _check("模型角色列表包含 vision", "vision" in roles, str(resp.status_code))
        if "vision" in roles:
            ok &= _check(
                "vision 角色是 chat 类型且有中文名",
                roles["vision"].get("required_type") == "chat"
                and bool(roles["vision"].get("label")),
                roles["vision"].get("label", ""),
            )

        # 2) 起始状态：没配 vision 角色时能力接口不该说自己能读图
        before = client.get("/v1/models/capabilities").json().get("data") or {}
        had_vision_before = bool((before.get("vision") or {}).get("can_read_image"))
        ok &= _check(
            "能力接口暴露 vision 块", "vision" in before, str(sorted(before.keys()))
        )

        # 3) 新建一个声明 supports_vision 的供应商
        create = client.post(
            "/v1/models/providers",
            headers=headers,
            json={
                "display_name": STUB_NAME,
                "provider_type": "chat",
                "provider": "openai_compatible",
                "base_url": stub_url,
                "api_key": "live-check",
                "model_name": "live-check-vl",
                "extra_config": {"supports_vision": True, "max_tokens": 2048},
                "is_active": True,
            },
        )
        ok &= _check("新建多模态供应商", create.status_code == 200, create.text[:200])
        provider_id = ((create.json().get("data") or {}).get("provider_id")) or ""
        if not provider_id:
            return 0 if ok else 1

        # supports_vision 真的落库了（不是被 extra_config 白名单吃掉）
        listed = client.get("/v1/models/providers", headers=headers).json().get("data") or []
        row = next((p for p in listed if p["provider_id"] == provider_id), None)
        ok &= _check(
            "supports_vision 落进 extra_config",
            bool(row and (row.get("extra_config") or {}).get("supports_vision")),
            str((row or {}).get("extra_config")),
        )

        # 4) 指派给 vision 角色
        assign = client.put(
            "/v1/models/roles/vision",
            headers=headers,
            json={"provider_id": provider_id},
        )
        ok &= _check("把供应商指派给 vision 角色", assign.status_code == 200, assign.text[:200])

        # 5) 能力接口 + 视觉桥自身都应认为可以读图了
        from core.services.model_config import ModelConfigService
        from core.vision import is_available, resolve_vision_config

        ModelConfigService.get_instance().invalidate_cache()
        after = client.get("/v1/models/capabilities").json().get("data") or {}
        vision_block = after.get("vision") or {}
        ok &= _check(
            "能力接口报告 can_read_image=true",
            bool(vision_block.get("can_read_image")),
            str(vision_block),
        )
        ok &= _check("视觉桥认为自己可用", is_available())
        cfg = resolve_vision_config()
        ok &= _check(
            "解析到的正是刚配的模型",
            bool(cfg and cfg.model_name == "live-check-vl"),
            cfg.model_name if cfg else "None",
        )

        # 6) 主模型是纯文本 → 应当挂上 view_image 工具
        from core.llm.agent_factory import _vision_bridge_needed

        ok &= _check("纯文本主模型下会注册 view_image", _vision_bridge_needed())

        # 7) 真的经这条角色配置识一次图 —— 证明「配得上」也「调得通」
        import asyncio

        from core.vision import get_vision_bridge, render_evidence

        image = _sample_png()
        result = asyncio.run(
            get_vision_bridge().describe(image, "image/png", use_cache=False)
        )
        ok &= _check("经 vision 角色真实调用一次识图", result is not None)
        if result is not None:
            rendered = render_evidence(result.evidence, name="live.png", model=result.model)
            ok &= _check(
                "识别结果来自刚配的模型且内容对得上",
                result.model == "live-check-vl" and "size=200x120" in rendered,
                f"model={result.model}",
            )
            ok &= _check("注入文本带不可信输入围栏", "不可信" in rendered)

    finally:
        server.shutdown()
        server.server_close()
        if provider_id:
            # 先摘角色再删供应商，避免留下悬空指派
            try:
                client.delete("/v1/models/roles/vision", headers=headers)
            except Exception:  # noqa: BLE001
                pass
            client.delete(f"/v1/models/providers/{provider_id}", headers=headers)
            from core.services.model_config import ModelConfigService

            ModelConfigService.get_instance().invalidate_cache()
            restored = client.get("/v1/models/capabilities").json().get("data") or {}
            print(
                "cleanup: 供应商已删除；can_read_image 回到",
                (restored.get("vision") or {}).get("can_read_image"),
            )

    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
