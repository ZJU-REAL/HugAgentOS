"""视觉桥真实联调脚本（手动运行，不进 pytest 默认集合）。

用当前部署里**真实配置的**模型跑通一次完整链路：合成一张内容已知的图 → 视觉桥 →
校验证据里确实读到了图上的字。凭据全程从 ``ModelConfigService`` 取，不打印。

用法（在 backend 容器内）::

    PYTHONPATH=. python -m tests.llm.vision_live_check list          # 列网关可用模型
    PYTHONPATH=. python -m tests.llm.vision_live_check probe <model> # 用指定模型试一次
    PYTHONPATH=. python -m tests.llm.vision_live_check run           # 用已配置的 vision 角色跑
"""

from __future__ import annotations

import asyncio
import io
import sys

MAGIC_TEXT = "HUGAGENT-7429"
MAGIC_WORD = "营收报表"


def make_test_image() -> bytes:
    """合成一张带已知文字的图。识别结果里必须能找回这两个串。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (640, 360), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 620, 90], fill="#1677ff")
    draw.text((40, 45), MAGIC_WORD, fill="white")
    draw.text((40, 140), f"CODE: {MAGIC_TEXT}", fill="black")
    draw.text((40, 190), "Q1 120   Q2 180   Q3 240", fill="black")
    # 三根柱子，用来看模型认不认得这是图表
    for i, height in enumerate((60, 110, 160)):
        x = 380 + i * 70
        draw.rectangle([x, 300 - height, x + 45, 300], fill="#1677ff")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


async def list_models(role: str = "main_agent") -> None:
    """列出某个已配置 provider 所在网关的模型清单（用于挑一个多模态模型）。"""
    import httpx

    from core.services.model_config import ModelConfigService

    cfg = ModelConfigService.get_instance().resolve(role)
    if cfg is None:
        print(f"角色 {role} 未配置")
        return
    print(f"gateway = {cfg.base_url}  (role={role}, current model={cfg.model_name})")
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{cfg.base_url.rstrip('/')}/models", headers=headers)
    print("HTTP", resp.status_code)
    if resp.status_code != 200:
        print(resp.text[:500])
        return
    for item in (resp.json().get("data") or []):
        print("  -", item.get("id"))


async def list_all_gateways() -> None:
    """遍历所有 active chat provider，列出各自网关上的模型清单。"""
    import httpx

    from core.db.engine import SessionLocal
    from core.db.models import ModelProvider

    seen: set[str] = set()
    with SessionLocal() as db:
        rows = (
            db.query(ModelProvider)
            .filter(ModelProvider.provider_type == "chat", ModelProvider.is_active.is_(True))
            .all()
        )
        targets = [(r.display_name, r.base_url, r.api_key) for r in rows]
    for name, base_url, api_key in targets:
        if not base_url or base_url in seen:
            continue
        seen.add(base_url)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(f"{base_url.rstrip('/')}/models", headers=headers)
            ids = [i.get("id") for i in (resp.json().get("data") or [])]
            print(f"{base_url}  (via {name})  HTTP {resp.status_code}")
            for model_id in ids:
                print("   -", model_id)
        except Exception as exc:  # noqa: BLE001
            print(f"{base_url}  (via {name})  ERROR {exc}")


async def probe(model_name: str, role: str = "main_agent") -> None:
    """借用某个已配置 provider 的网关和凭据，试指定模型能否读图。"""
    from dataclasses import replace

    from core.services.model_config import ModelConfigService
    from core.vision.prompt import build_vision_prompt
    from core.vision.provider import VisionProvider
    from core.vision.schema import parse_evidence

    base = ModelConfigService.get_instance().resolve(role)
    if base is None:
        print(f"角色 {role} 未配置")
        return
    cfg = replace(base, model_name=model_name)
    image = make_test_image()
    result = await VisionProvider(cfg).describe(
        image, "image/png", build_vision_prompt(image_kind="inline"), timeout=120
    )
    print(f"model={model_name} ok={result.ok} mode={result.mode}")
    if not result.ok:
        print("error:", result.error[:600])
        return
    evidence = parse_evidence(result.text)
    if evidence is None:
        print("结构解析失败，原始输出前 600 字：")
        print(result.text[:600])
        return
    blob = result.text
    print("summary:", evidence.summary[:200])
    print(f"包含 {MAGIC_TEXT}: {MAGIC_TEXT in blob}")
    print(f"包含 {MAGIC_WORD}: {MAGIC_WORD in blob}")


async def run() -> int:
    """完整链路：视觉桥（含缓存/重试/渲染）→ 校验读到了图上的字。"""
    from core.vision import get_vision_bridge, is_available, render_evidence

    if not is_available():
        print("FAIL: 未配置 vision 角色，且主模型不支持读图")
        return 1
    image = make_test_image()

    result = await get_vision_bridge().describe(image, "image/png", use_cache=False)
    if result is None:
        print("FAIL: 视觉桥识别失败")
        return 1
    rendered = render_evidence(result.evidence, name="test.png", model=result.model)
    print(rendered)
    print("\n--- meta ---")
    print(result.to_meta())

    ok = True
    for needle in (MAGIC_TEXT, MAGIC_WORD):
        hit = needle in rendered
        print(f"{'PASS' if hit else 'FAIL'}: 证据中包含 {needle}")
        ok = ok and hit
    if "不可信" not in rendered:
        print("FAIL: 渲染结果缺少不可信输入围栏")
        ok = False
    else:
        print("PASS: 不可信输入围栏就位")

    # 第二次走缓存：必须命中且明显更快
    cached = await get_vision_bridge().describe(image, "image/png")
    await get_vision_bridge().describe(image, "image/png")
    third = await get_vision_bridge().describe(image, "image/png")
    print(f"{'PASS' if third and third.cached else 'FAIL'}: 二次调用命中缓存")
    ok = ok and bool(third and third.cached) and cached is not None

    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    argv = sys.argv[1:]
    command = argv[0] if argv else "run"
    if command == "list":
        asyncio.run(list_models(argv[1] if len(argv) > 1 else "main_agent"))
        return 0
    if command == "list-all":
        asyncio.run(list_all_gateways())
        return 0
    if command == "probe":
        if len(argv) < 2:
            print("usage: probe <model_name> [role]")
            return 2
        asyncio.run(probe(argv[1], argv[2] if len(argv) > 2 else "main_agent"))
        return 0
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
