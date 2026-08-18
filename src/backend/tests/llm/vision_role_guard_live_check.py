"""实测「视觉桥角色只能指多模态模型」这道闸（手动运行）。

打真实 DB 和真实 `/v1/models/*` 路由，用**本机已有的纯文本模型**去指派 vision 角色，
断言被 400 拒绝且报错说得清怎么修；再确认角色列表把 `requires_capability` 暴露给了前端。

不创建任何数据，也不改动现有配置（失败路径本来就不会落库）。

用法（backend 容器内）::

    python -m tests.llm.vision_role_guard_live_check
"""

from __future__ import annotations

import os
import sys


def _check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}: {label}{(' — ' + detail) if detail else ''}", flush=True)
    return ok


def main() -> int:
    from fastapi.testclient import TestClient

    from api.app import app

    token = (os.getenv("CONFIG_TOKEN") or os.getenv("ADMIN_TOKEN") or "").strip().strip('"').strip("'")
    if not token:
        raise SystemExit("环境里没有 CONFIG_TOKEN / ADMIN_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}
    ok = True

    with TestClient(app) as client:
        roles = {
            r["role_key"]: r
            for r in (client.get("/v1/models/roles", headers=headers).json().get("data") or [])
        }
        ok &= _check(
            "角色列表暴露 requires_capability",
            roles.get("vision", {}).get("requires_capability") == "supports_vision",
            str(roles.get("vision", {}).get("requires_capability")),
        )
        ok &= _check(
            "其它角色不带能力位要求",
            roles.get("main_agent", {}).get("requires_capability") is None,
        )

        providers = client.get("/v1/models/providers", headers=headers).json().get("data") or []
        chat = [p for p in providers if p.get("provider_type") == "chat"]
        text_only = [p for p in chat if not (p.get("extra_config") or {}).get("supports_vision")]
        multimodal = [p for p in chat if (p.get("extra_config") or {}).get("supports_vision")]
        print(f"  本机 chat 供应商 {len(chat)} 个：纯文本 {len(text_only)}，多模态 {len(multimodal)}")

        if not text_only:
            print("  跳过：本机没有纯文本 chat 供应商可用于负例")
            return 0 if ok else 1

        victim = text_only[0]
        resp = client.put(
            "/v1/models/roles/vision",
            headers=headers,
            json={"provider_id": victim["provider_id"]},
        )
        ok &= _check(
            f"纯文本模型「{victim['display_name']}」指派 vision 被拒",
            resp.status_code == 400,
            f"HTTP {resp.status_code}",
        )
        detail = str((resp.json() or {}).get("detail", ""))
        ok &= _check("报错说清了怎么修", "支持读图" in detail, detail[:160])

        # 拒绝之后不能留下半截分配
        after = {
            r["role_key"]: r
            for r in (client.get("/v1/models/roles", headers=headers).json().get("data") or [])
        }
        ok &= _check(
            "被拒后 vision 角色未被写入",
            after.get("vision", {}).get("provider_id") in (None, ""),
            str(after.get("vision", {}).get("provider_id")),
        )

        # 正例：同一条路对多模态供应商应放行（本机若没有则跳过，不伪造）
        if multimodal:
            target = multimodal[0]
            allow = client.put(
                "/v1/models/roles/vision",
                headers=headers,
                json={"provider_id": target["provider_id"]},
            )
            ok &= _check(
                f"多模态模型「{target['display_name']}」可以指派",
                allow.status_code == 200,
                f"HTTP {allow.status_code}",
            )
            client.delete("/v1/models/roles/vision", headers=headers)
            print("  已还原：vision 角色回到未分配")
        else:
            print("  跳过正例：本机暂无勾选了「支持读图」的供应商")

    print("\nRESULT:", "PASS" if ok else "FAIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
