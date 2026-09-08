#!/usr/bin/env python3
"""桌面双端（云端 + 本机执行面）能力安装端到端验收。

前提（由调用方准备）：
  * 一个"云端"后端：完整树代码，Postgres，AUTH_MODE=mock（Bearer <name> 即该用户），
    通过 ``scripts/desktop_cloud_wrapper.py`` 挂在 ``/api`` 之下；
  * 一个"本机"后端：``cli.py serve``（local 档位），注入 HUGAGENT_DESKTOP_BRIDGE_SECRET /
    CONFIG_TOKEN / HUGAGENT_CAPS_ROOT；
  * 本机能访问 ``docker exec <pg-container> psql``（云端库的能力位授予与断言用）。

场景（每一步落到 JSON 报告，不以聊天正文为准）：
  01 云端建技能 / 智能体（用户 A），签发 capability token
  02 壳侧动作：模型清单 → 本机导入；桥配置推送
  03 登录后的这一次同步即全部就绪：技能/智能体/插件已落盘、存储层 revision、
     运行视图联接、mcp.json 投影（没有「待下载」这一步，也没有手动准备入口）
  05 真实调用：本机对话经云端模型网关执行技能脚本，读回标记
  06 同名冲突：本机私有同名技能 → conflict → 视图自动隐藏（不需要用户选择）
  07 只属于本机的技能与云端能力并存
  08 切换账号 B：A 的文件留在 A 的 profile，B 的视图不含 A 的私有技能
  09 云端不可达：sync 返回 cloud_unavailable，本机副本仍可用
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import secrets
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import httpx


class Report:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.steps: List[Dict[str, Any]] = []
        self.started = time.time()

    def step(self, name: str, ok: bool, **detail: Any) -> None:
        entry = {"step": name, "ok": bool(ok), "at": round(time.time() - self.started, 1), **detail}
        self.steps.append(entry)
        flag = "PASS" if ok else "FAIL"
        print(f"[{flag}] {name} {json.dumps(detail, ensure_ascii=False)[:300]}", flush=True)
        self.flush()

    def flush(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "started_at": self.started,
                    "passed": sum(1 for s in self.steps if s["ok"]),
                    "failed": sum(1 for s in self.steps if not s["ok"]),
                    "steps": self.steps,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def _b64(obj: Dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")


def _skill_zip(skill_id: str, marker: str, body_note: str) -> bytes:
    md = (
        f"---\nname: {skill_id}\ndescription: 端到端验收技能：读取标记文件并原样回复（{body_note}）\n---\n"
        f"# {skill_id}\n\n当用户要求「读取标记」时：\n\n"
        f"1. 用 bash 工具执行 `cat /workspace/skills/{skill_id}/marker.txt`\n"
        f"2. 把命令输出**原样**回复给用户，不要改写、不要加解释。\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{skill_id}/SKILL.md", md)
        zf.writestr(f"{skill_id}/marker.txt", marker)
    return buf.getvalue()


class Cloud:
    def __init__(
        self, base: str, pg_container: str, cloud_container: str, host: str, device_id: str
    ) -> None:
        self.container, self.device_id = cloud_container, device_id
        self.fixture_secret = secrets.token_urlsafe(32)
        code = 'import os,sys; from pathlib import Path; p=Path("/tmp/capability-e2e-fixture.secret"); p.write_text(sys.stdin.read()); os.chmod(p,0o600)'
        subprocess.run(
            ["docker", "exec", "-i", self.container, "python", "-c", code],
            input=self.fixture_secret,
            text=True,
            capture_output=True,
            check=True,
        )
        self.base = base.rstrip("/")
        self.pg = pg_container
        self.http = httpx.Client(timeout=60.0, headers={"Host": host}, trust_env=False)

    def h(self, user: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {user}"}

    def me(self, user: str) -> Dict[str, Any]:
        r = self.http.get(f"{self.base}/api/v1/me", headers=self.h(user))
        r.raise_for_status()
        return r.json()["data"]

    def psql(self, db: str, sql: str) -> str:
        out = subprocess.run(
            ["docker", "exec", self.pg, "psql", "-U", "hugagent_user", "-d", db, "-Atc", sql],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()

    def grant_capabilities(self, db: str, user_center_id: str) -> None:
        # ORM attribute ``extra_data`` is the ``metadata`` JSONB column.
        self.psql(
            db,
            "update users_shadow set metadata = coalesce(metadata, '{}'::jsonb) || "
            '\'{"can_add_skill": true, "can_add_agent": true, "can_add_mcp": true, "can_import_plugin": true}\'::jsonb '
            f"where user_center_id = '{user_center_id}'",
        )

    def upload_skill(self, user: str, data: bytes) -> Dict[str, Any]:
        r = self.http.post(
            f"{self.base}/api/v1/me/skills/upload",
            headers=self.h(user),
            files={"file": ("skill.zip", data, "application/zip")},
        )
        r.raise_for_status()
        return r.json()["data"]

    def delete_skill(self, user: str, skill_id: str) -> None:
        self.http.delete(f"{self.base}/api/v1/me/skills/{skill_id}", headers=self.h(user))

    def create_agent(
        self, user: str, name: str, prompt: str, skill_ids: List[str], mcp_ids=None, plugin_ids=None
    ) -> Dict[str, Any]:
        r = self.http.post(
            f"{self.base}/api/v1/agents",
            headers=self.h(user),
            json={
                "name": name,
                "description": "e2e",
                "system_prompt": prompt,
                "skill_ids": skill_ids,
                "mcp_server_ids": mcp_ids or [],
                "plugin_ids": plugin_ids or [],
            },
        )
        r.raise_for_status()
        return r.json()["data"]

    def private_plugin(self, user: str, run_id: str):
        secret = "E2E_CANARY_" + secrets.token_hex(16)
        marker = "PRIVATE_" + secrets.token_hex(8)
        config = json.dumps({"secret": secret, "marker": marker})
        code = "import os,sys; from pathlib import Path; p=Path('/tmp/capability-e2e-mcp.json'); p.write_text(sys.stdin.read()); os.chmod(p,0o600)"
        subprocess.run(
            ["docker", "exec", "-i", self.container, "python", "-c", code],
            input=config,
            text=True,
            capture_output=True,
            check=True,
        )
        probe = subprocess.run(
            [
                "docker",
                "exec",
                self.container,
                "python",
                "-c",
                "import socket; s=socket.create_connection(('127.0.0.1',31999),timeout=1); s.close()",
            ],
            capture_output=True,
        )
        if probe.returncode:
            subprocess.run(
                [
                    "docker",
                    "exec",
                    "-d",
                    self.container,
                    "python",
                    "/app/src/backend/scripts/desktop_fixture_mcp.py",
                ],
                capture_output=True,
                check=True,
            )
            for _ in range(20):
                time.sleep(0.2)
                probe = subprocess.run(
                    [
                        "docker",
                        "exec",
                        self.container,
                        "python",
                        "-c",
                        "import socket; s=socket.create_connection(('127.0.0.1',31999),timeout=1); s.close()",
                    ],
                    capture_output=True,
                )
                if probe.returncode == 0:
                    break
            if probe.returncode:
                raise RuntimeError("private fixture MCP did not start")
        slug, skill_id = "e2e-pack-" + run_id, "e2e-pack-skill-" + run_id
        pack_marker = "PLUGIN_" + secrets.token_hex(8)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            z.writestr(
                ".claude-plugin/plugin.json",
                json.dumps(
                    {"name": slug, "version": "1.0.0", "description": "isolated desktop acceptance"}
                ),
            )
            z.writestr(
                ".mcp.json",
                json.dumps(
                    {
                        "mcpServers": {
                            "e2e_private": {
                                "url": "http://127.0.0.1:31999/mcp",
                                "headers": {"X-Fixture-Key": secret},
                            }
                        }
                    }
                ),
            )
            z.writestr(
                f"skills/{skill_id}/SKILL.md",
                f"---\nname: {skill_id}\ndescription: Read the plugin marker\n---\nUse bash to read ${{CLAUDE_PLUGIN_ROOT}}/marker.txt and return it verbatim.\n",
            )
            z.writestr(f"skills/{skill_id}/marker.txt", pack_marker)
        response = self.http.post(
            f"{self.base}/api/v1/plugins/import",
            headers=self.h(user),
            files={"file": ("plugin.zip", buffer.getvalue(), "application/zip")},
        )
        response.raise_for_status()
        result = response.json()["data"]
        # Import returns an installation receipt, not its component IDs. Resolve
        # the actual installed projection before preparing or binding anything.
        installed = self.http.get(f"{self.base}/api/v1/plugins/installed", headers=self.h(user))
        installed.raise_for_status()
        projection = next(
            item
            for item in installed.json()["data"]["items"]
            if item["install_id"] == result["install_id"]
        )
        result = {**result, "skills": projection["skills"], "mcp": projection["mcp"]}
        if not result["skills"] or not result["mcp"]:
            raise RuntimeError("fixture plugin must contain both skill and MCP components")
        # Discover the private endpoint through the same cloud-side service used
        # by MCP connection testing. A plugin config alone does not contain a
        # tools/list snapshot and must never be reported as runtime-ready.
        self.probe_fixture_mcps(result.get("mcp") or [])
        return result, secret, marker, pack_marker

    def probe_fixture_mcps(self, server_ids):
        code = """import asyncio,json,sys
from core.db.engine import SessionLocal
from core.db.models import AdminMcpServer
from core.services.mcp_management_service import probe_mcp_connectivity
async def main():
    with SessionLocal() as db:
        for sid in json.loads(sys.stdin.read()):
            assert sid.startswith('e2e-pack-')
            row=db.get(AdminMcpServer,sid)
            ok,_=await probe_mcp_connectivity(row,db,timeout_seconds=15)
            if not ok or not row.tools_json:
                raise RuntimeError('fixture MCP discovery failed')
        db.commit()
asyncio.run(main())
"""
        result = subprocess.run(
            ["docker", "exec", "-i", self.container, "python", "-c", code],
            input=json.dumps(list(server_ids)),
            capture_output=True,
            text=True,
            timeout=45,
        )
        if result.returncode:
            raise RuntimeError("fixture MCP discovery failed")

    def token(self, user: str) -> Dict[str, Any]:
        me = self.me(user)
        # Mint through the isolated wrapper so in-memory sessions exist in the
        # same cloud process that will validate them; this never ships in api.app.
        fixture = self.http.post(
            f"{self.base}/__e2e/session",
            headers={"Authorization": "Bearer " + self.fixture_secret},
            json={"user_center_id": user},
        )
        fixture.raise_for_status()
        session = fixture.json()
        r = self.http.post(
            f"{self.base}/api/v1/desktop/capability/token",
            json={"device_id": self.device_id},
            cookies={session["cookie"]: session["token"]},
        )
        r.raise_for_status()
        return r.json()["data"]

    def models(self, token: str) -> Dict[str, Any]:
        r = self.http.get(
            f"{self.base}/api/v1/desktop/capability/models",
            headers={"Authorization": f"Bearer {token}", "X-Desktop-Device-Id": self.device_id},
        )
        r.raise_for_status()
        return r.json()["data"]


def shell_user_center_id(cloud_base: str, user_center_id: str) -> str:
    """与桌面壳 / 本机后端相同的桥接用户命名空间规则。"""
    parts = urlsplit(cloud_base.strip())
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"cloud:{parts.hostname or 'cloud'}:{port}:{user_center_id}"


class Device:
    def __init__(self, base: str, bridge_secret: str, config_token: str, device_id: str, cloud_base: str) -> None:
        self.base = base.rstrip("/")
        self.cloud_base = cloud_base.rstrip("/")
        self.device_id = device_id
        self.secret = bridge_secret
        self.config_token = config_token
        self.http = httpx.Client(timeout=120.0, trust_env=False)

    def h(self, user_center_id: str) -> Dict[str, str]:
        namespaced = shell_user_center_id(self.cloud_base, user_center_id)
        return {
            "X-Desktop-Bridge": self.secret,
            "X-Desktop-Bridge-User": _b64(
                {"user_center_id": namespaced, "username": user_center_id}
            ),
        }

    def config_h(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.config_token}"}

    def push_bridge(self, cloud_base: str, token: str, expires_in: int) -> None:
        r = self.http.post(
            f"{self.base}/v1/desktop/capability/cloud-bridge",
            headers={"Authorization": "Bearer " + self.secret},
            json={
                "cloud_base": cloud_base,
                "token": token,
                "expires_in": expires_in,
                "device_id": self.device_id,
            },
        )
        r.raise_for_status()

    def import_models(
        self, manifest: Dict[str, Any], cloud_base: str, token: str
    ) -> Dict[str, Any]:
        providers = []
        for p in manifest["providers"]:
            p = dict(p)
            p["base_url"] = (
                f"{cloud_base}/api/v1/desktop/capability/gateway/models/{p['provider_id']}"
            )
            p["api_key"] = token
            providers.append(p)
        r = self.http.post(
            f"{self.base}/v1/models/import",
            headers={"Authorization": "Bearer " + self.secret},
            json={
                "providers": providers,
                "role_assignments": manifest["role_assignments"],
                "overwrite": True,
            },
        )
        r.raise_for_status()
        return r.json()["data"]

    def call(self, user: str, method: str, path: str, **kw: Any) -> httpx.Response:
        return self.http.request(method, f"{self.base}{path}", headers=self.h(user), **kw)

    def json(self, user: str, method: str, path: str, **kw: Any) -> Dict[str, Any]:
        r = self.call(user, method, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:400]}")
        return r.json()["data"]

    def me(self, user: str) -> Dict[str, Any]:
        return self.json(user, "GET", "/v1/me")

    def upload_skill(self, user: str, data: bytes) -> Dict[str, Any]:
        r = self.http.post(
            f"{self.base}/v1/me/skills/upload",
            headers=self.h(user),
            files={"file": ("skill.zip", data, "application/zip")},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"upload -> {r.status_code}: {r.text[:400]}")
        return r.json()["data"]

    def chat(
        self,
        user: str,
        message: str,
        timeout_s: float = 240.0,
        agent_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        **selection: Any,
    ) -> Dict[str, Any]:
        if chat_id is None:
            chat = self.json(user, "POST", "/v1/chats", json={"title": "e2e"})
            chat_id = chat["chat_id"] if "chat_id" in chat else chat["id"]
        text: List[str] = []
        tool_calls: List[str] = []
        errors: List[str] = []
        event_types = set()
        deadline = time.time() + timeout_s
        with self.http.stream(
            "POST",
            f"{self.base}/v1/chats/stream",
            headers={**self.h(user), "Accept": "text/event-stream"},
            json={"chat_id": chat_id, "message": message, "agent_id": agent_id, **selection},
            timeout=timeout_s,
        ) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"stream -> {r.status_code}: {r.read()[:400]!r}")
            event = ""
            for line in r.iter_lines():
                if time.time() > deadline:
                    errors.append("timeout")
                    break
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                event_types.add(str(obj.get("type") or obj.get("event") or event))
                if event in ("ai_message", "content") or obj.get("event") == "ai_message":
                    delta = obj.get("delta") or obj.get("content") or obj.get("text") or ""
                    if isinstance(delta, str):
                        text.append(delta)
                elif (
                    event == "tool_call"
                    or obj.get("type") == "tool_call"
                    or obj.get("event") == "tool_call"
                ):
                    tool_calls.append(str(obj.get("name") or obj.get("tool_name") or obj)[:80])
                elif event == "error" or obj.get("type") == "error" or obj.get("event") == "error":
                    errors.append(json.dumps(obj, ensure_ascii=False)[:300])
        return {
            "chat_id": chat_id,
            "text": "".join(text),
            "tool_calls": tool_calls,
            "errors": errors,
            "event_types": sorted(event_types),
        }


def _find_user_view(device_home: Path, name: str, user_id: str) -> Optional[Path]:
    root = device_home / "workspace" / "skills_u" / user_id
    candidate = root / name
    return candidate if candidate.is_symlink() or candidate.exists() else None


def _read_view_marker(view: Optional[Path], device_home: Path) -> Optional[str]:
    if view is None:
        return None
    relative = str(view.relative_to(device_home) / "marker.txt")
    code = (
        "from pathlib import Path; import os; p=Path(os.environ['HUGAGENT_HOME'])/"
        + repr(relative)
        + "; print(p.read_text(), end='')"
    )
    result = subprocess.run(
        ["docker", "exec", "capdevice", "python", "-c", code], capture_output=True, text=True
    )
    return result.stdout if result.returncode == 0 else None


def _execute_local_marker(user_id: str, skill_id: str, expected: str) -> bool:
    """Exercise the actual device runner and pinned local view while cloud is down."""
    code = """import asyncio,json,os,sys
from cli import apply_local_env
apply_local_env(port=int(os.environ.get("BACKEND_PORT") or "32101"))
from core.capabilities import runtime
from core.sandbox.protocol import ExecuteRequest
from core.sandbox.script_runner_provider import ScriptRunnerProvider
async def main():
    values=json.loads(sys.stdin.read())
    run_id="offline-"+values["skill_id"]
    run=runtime.prepare(run_id,values["user_id"],skill_ids=[values["skill_id"]])
    runtime.preflight(run,skill_ids=[values["skill_id"]])
    result=await ScriptRunnerProvider().execute(ExecuteRequest(
        script_content="from pathlib import Path\\nprint(Path("+repr("/workspace/skills/"+values["skill_id"]+"/marker.txt")+").read_text())",
        script_name="offline_marker.py", user_id=values["user_id"],
        session_id=run_id,capability_run_id=run_id,timeout=15))
    print(json.dumps({"offline_marker_ok":run.profile is None and result.exit_code==0 and result.stdout.strip()==values["expected"]}))
asyncio.run(main())
"""
    result = subprocess.run(
        ["docker", "exec", "-i", "capdevice", "python", "-c", code],
        input=json.dumps({"user_id": user_id, "skill_id": skill_id, "expected": expected}),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        return False
    return any(line.strip() == '{"offline_marker_ok": true}' for line in result.stdout.splitlines())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--cloud", required=True, help="云端根地址（含 /api 之前的部分），如 http://127.0.0.1:32201"
    )
    ap.add_argument(
        "--cloud-internal",
        default=None,
        help="本机进程访问云端用的地址（本机跑在容器里时与 --cloud 不同）；缺省同 --cloud",
    )
    ap.add_argument("--device", required=True, help="本机后端地址，如 http://127.0.0.1:32101")
    ap.add_argument("--bridge-secret", required=True)
    ap.add_argument("--config-token", required=True)
    ap.add_argument("--device-root", required=True, help="HUGAGENT_CAPS_ROOT")
    ap.add_argument("--device-home", required=True, help="HUGAGENT_HOME")
    ap.add_argument("--pg-container", default="hugagent-postgres")
    ap.add_argument("--cloud-db", default="hugagent_captest")
    ap.add_argument("--cloud-container", default="capcloud")
    ap.add_argument("--run-id", default=secrets.token_hex(4))
    ap.add_argument("--report", required=True)
    ap.add_argument("--skip-model", action="store_true", help="跳过真实模型调用步骤")
    args = ap.parse_args()

    report = Report(Path(args.report))
    cloud_internal = (args.cloud_internal or args.cloud).rstrip("/")
    device_id = "e2e-" + args.run_id
    cloud = Cloud(
        args.cloud,
        args.pg_container,
        args.cloud_container,
        urlsplit(cloud_internal).netloc,
        device_id,
    )
    device = Device(args.device, args.bridge_secret, args.config_token, device_id, cloud_internal)
    device_root = Path(args.device_root)
    device_home = Path(args.device_home)
    user_a, user_b = "capuser-a-" + args.run_id, "capuser-b-" + args.run_id
    skill_id = "e2e-marker-" + args.run_id
    agent_name = "E2E 标记读取员 " + args.run_id
    marker = f"MARKER-{secrets.token_hex(6)}"

    # ── 00 健康 ─────────────────────────────────────────────────────────
    try:
        ch = cloud.http.get(f"{args.cloud}/api/health").status_code
        dh = device.http.get(f"{args.device}/health").status_code
        report.step("00 health", ch == 200 and dh == 200, cloud=ch, device=dh)
        if ch != 200 or dh != 200:
            return 1
    except Exception as exc:  # noqa: BLE001
        report.step("00 health", False, error=str(exc))
        return 1

    # ── 01 云端夹具 ──────────────────────────────────────────────────────
    try:
        me_a = cloud.me(user_a)
        cloud.grant_capabilities(args.cloud_db, user_a)
        cloud.me(user_b)
        cloud.delete_skill(user_a, skill_id)
        skill = cloud.upload_skill(user_a, _skill_zip(skill_id, marker, "cloud"))
        cloud_skill_id = skill.get("id") or skill_id
        plugin, canary, private_marker, plugin_marker = cloud.private_plugin(user_a, args.run_id)
        plugin_skills = list(plugin.get("skills") or [])
        plugin_mcps = list(plugin.get("mcp") or [])
        agent = cloud.create_agent(
            user_a,
            agent_name,
            "你是标记读取员。使用提供的技能和MCP工具读取标记，严禁猜测。",
            [cloud_skill_id] + plugin_skills,
            mcp_ids=plugin_mcps,
            plugin_ids=[plugin["install_id"]],
        )
        tok = cloud.token(user_a)
        report.step(
            "01 cloud fixtures",
            bool(tok.get("token")) and bool(agent.get("agent_id")),
            cloud_user_id=me_a.get("user_id"),
            skill_id=cloud_skill_id,
            agent_id=agent.get("agent_id"),
            token_expires_in=tok.get("expires_in"),
        )
    except Exception as exc:  # noqa: BLE001
        report.step("01 cloud fixtures", False, error=str(exc)[:500])
        return 1
    token_a = tok["token"]

    # ── 02 壳侧动作：模型导入 + 桥推送 ───────────────────────────────────
    try:
        manifest = cloud.models(token_a)
        imported = device.import_models(manifest, cloud_internal, token_a)
        device.push_bridge(cloud_internal, token_a, int(tok.get("expires_in") or 600))
        local_user_a = str(device.me(user_a)["user_id"])
        report.step(
            "02 shell: models + bridge",
            True,
            providers=len(manifest.get("providers", [])),
            imported=imported,
        )
    except Exception as exc:  # noqa: BLE001
        report.step("02 shell: models + bridge", False, error=str(exc)[:500])
        return 1

    # ── 03 同步即就绪（登录后的这一次同步就把账号能力全部准备好）─────────
    try:
        st = device.json(user_a, "POST", "/v1/desktop/capabilities/sync")
        skills_list = device.json(
            user_a, "GET", "/v1/desktop/capabilities/installations?kind=skill"
        )
        by_name = {i["runtime_name"]: i for i in skills_list["items"] if i["source"] == "cloud"}
        cloud_skill = by_name.get(cloud_skill_id)
        agents_list = device.json(
            user_a, "GET", "/v1/desktop/capabilities/installations?kind=agent"
        )
        cloud_agents = [i for i in agents_list["items"] if i["source"] == "cloud"]
        plugins_list = device.json(
            user_a, "GET", "/v1/desktop/capabilities/installations?kind=plugin"
        )
        mcp_json = device.json(user_a, "GET", "/v1/desktop/capabilities/mcp-json")
        profile = skills_list["profile_id"]
        managed = mcp_json["managedProfiles"].get(profile or "", {})
        agent_ready = any(
            i["display_name"] == agent_name and i["state"] == "ready" for i in cloud_agents
        )
        agent_dir = device_root / "agents" / (profile or "?")
        plugin_skills_ready = all(
            any(
                i["runtime_name"] == sid and i["state"] == "ready"
                for i in skills_list["items"]
            )
            for sid in plugin_skills
        )
        rev = (cloud_skill or {}).get("revision")
        store_dir = device_root / "skills" / (profile or "?") / cloud_skill_id / (rev or "?")
        view_link = _find_user_view(device_home, cloud_skill_id, local_user_a)
        link_ok = bool(
            view_link
            and os.path.islink(view_link)
            and _read_view_marker(view_link, device_home) == marker
        )
        chosen = [
            i
            for i in skills_list["items"]
            if i["runtime_name"] == cloud_skill_id and i["resolution"]["outcome"] == "chosen"
        ]
        report.step(
            "03 sync leaves everything ready",
            cloud_skill is not None
            and cloud_skill["state"] == "ready"
            and st.get("pending_count") == 0
            and plugin_skills_ready
            and agent_ready
            and bool(managed.get("servers"))
            and store_dir.is_dir()
            and (store_dir / "marker.txt").read_text() == marker
            and link_ok
            and bool(chosen),
            profile=profile,
            skill_state=(cloud_skill or {}).get("state"),
            pending_count=st.get("pending_count"),
            installed_count=st.get("installed_count"),
            cloud_agents=len(cloud_agents),
            agent_ready=agent_ready,
            agent_files=sorted(p.name for p in agent_dir.iterdir()) if agent_dir.is_dir() else [],
            plugins=len(plugins_list["items"]),
            plugin_skills_ready=plugin_skills_ready,
            managed_servers=len(managed.get("servers") or {}),
            mcp_json_generation=mcp_json["generation"],
            revision=rev,
            store_dir=str(store_dir),
            view_link=str(view_link),
            chosen_source=chosen[0]["source"] if chosen else None,
        )
        if cloud_skill is None or cloud_skill["state"] != "ready":
            return 1
    except Exception as exc:  # noqa: BLE001
        report.step("03 sync leaves everything ready", False, error=str(exc)[:500])
        return 1

    # ── 05 真实调用 ─────────────────────────────────────────────────────
    if not args.skip_model:
        try:
            out = device.chat(
                user_a,
                f"请使用技能 {cloud_skill_id}：读取标记，并把标记内容原样回复我。",
                skill_id=cloud_skill_id,
            )
            got = marker in out["text"] and bool(out["tool_calls"]) and not out["errors"]
            session_links = sorted(
                str(p)
                for p in (device_home / "workspace" / ".sessions").glob("*/skills")
                if os.path.islink(p)
            )
            report.step(
                "05 real run via cloud model gateway",
                got,
                reply=out["text"][-300:],
                tool_calls=out["tool_calls"][:10],
                errors=out["errors"],
                session_skill_links=len(session_links),
            )
        except Exception as exc:  # noqa: BLE001
            report.step("05 real run via cloud model gateway", False, error=str(exc)[:500])

    # ── 05b 插件组件与服务器内网 MCP 的真实调用和凭据边界 ───────────────
    if not args.skip_model:
        try:
            out = device.chat(
                user_a,
                "请调用 e2e_read_private_marker 读取云内网标记，再用插件技能 "
                + ", ".join(plugin_skills)
                + " 读取本机插件标记。只回复两个标记。",
                agent_id=agent["agent_id"],
            )
            trace = subprocess.run(
                [
                    "docker",
                    "exec",
                    args.cloud_container,
                    "cat",
                    "/tmp/capability-e2e-mcp-trace.jsonl",
                ],
                capture_output=True,
                text=True,
            )
            direct = subprocess.run(
                [
                    "docker",
                    "exec",
                    "capdevice",
                    "python",
                    "-c",
                    "import socket; socket.create_connection(('127.0.0.1',31999),timeout=2)",
                ],
                capture_output=True,
            )
            report.step(
                "05b agent + plugin + private MCP",
                private_marker in out["text"]
                and plugin_marker in out["text"]
                and not out["errors"]
                and any("e2e_read_private_marker" in name for name in out["tool_calls"])
                and private_marker in trace.stdout
                and direct.returncode != 0,
                reply=out["text"][-300:],
                errors=out["errors"],
                tool_calls=out["tool_calls"],
                event_types=out["event_types"],
                cloud_trace=private_marker in trace.stdout,
                direct_connection_refused=direct.returncode != 0,
            )
            docs = [
                device.json(user_a, "GET", "/v1/desktop/capabilities/mcp-json"),
                cloud.models(token_a),
            ]
            files_leak = any(
                canary.encode() in p.read_bytes()
                for p in device_root.rglob("*")
                if p.is_file() and not p.is_symlink()
            )
            db_leak = canary.encode() in (device_home / "data.db").read_bytes()
            logs = subprocess.run(["docker", "logs", "capdevice"], capture_output=True, text=True)
            report.step(
                "05c credential boundary",
                not any(canary in json.dumps(doc) for doc in docs)
                and not files_leak
                and not db_leak
                and canary not in logs.stdout + logs.stderr,
                file_leak=files_leak,
                database_leak=db_leak,
                log_leak=canary in logs.stdout + logs.stderr,
            )
        except Exception as exc:
            report.step("05b agent + plugin + private MCP", False, error=str(exc)[:500])

    # Explicit plugin selection and its subsequent sticky activation use the
    # ordinary chat route, independently of an agent's static plugin bindings.
    if not args.skip_model:
        try:
            out = device.chat(
                user_a,
                "使用所选插件读取云内网标记和本机插件标记，只回复这两个标记。",
                plugin_id=plugin["install_id"],
            )
            report.step(
                "05d explicit plugin selection",
                private_marker in out["text"]
                and plugin_marker in out["text"]
                and not out["errors"]
                and any("e2e_read_private_marker" in name for name in out["tool_calls"]),
                reply=out["text"][-300:],
                errors=out["errors"],
                tool_calls=out["tool_calls"],
            )
            followup = device.chat(
                user_a,
                "再次实际调用上一轮的插件工具读取两个标记，不要直接复制之前的回答。",
                chat_id=out["chat_id"],
            )
            report.step(
                "05e sticky plugin next turn",
                private_marker in followup["text"]
                and plugin_marker in followup["text"]
                and not followup["errors"]
                and any("e2e_read_private_marker" in name for name in followup["tool_calls"]),
                reply=followup["text"][-300:],
                errors=followup["errors"],
                tool_calls=followup["tool_calls"],
            )
            connector = device.chat(
                user_a,
                "调用所选连接器的 e2e_read_private_marker，原样返回读取到的云内网标记。",
                connector_id=plugin_mcps[0],
            )
            report.step(
                "05f explicit connector selection",
                private_marker in connector["text"]
                and not connector["errors"]
                and any("e2e_read_private_marker" in name for name in connector["tool_calls"]),
                reply=connector["text"][-300:],
                errors=connector["errors"],
                tool_calls=connector["tool_calls"],
            )
        except Exception as exc:
            report.step("05d-f explicit and sticky selections", False, error=str(exc)[:500])

    # ── 06 同名冲突：自动隐藏，不需要用户做任何选择 ──────────────────────
    try:
        local_marker = f"LOCAL-{secrets.token_hex(4)}"
        device.upload_skill(user_a, _skill_zip(skill_id, local_marker, "local"))
        listing = device.json(user_a, "GET", "/v1/desktop/capabilities/installations?kind=skill")
        cands = [i for i in listing["items"] if i["runtime_name"] == cloud_skill_id]
        conflict = cloud_skill_id in listing["conflicts"]
        hidden_during_conflict = _find_user_view(device_home, cloud_skill_id, local_user_a) is None
        report.step(
            "06 name conflict is hidden automatically",
            conflict and hidden_during_conflict,
            candidates=[(c["profile"], c["resolution"]["outcome"]) for c in cands],
            conflict=conflict,
            hidden_during_conflict=hidden_during_conflict,
        )
    except Exception as exc:  # noqa: BLE001
        report.step("06 name conflict is hidden automatically", False, error=str(exc)[:500])

    # ── 07 只属于本机的技能：与云端能力并存，离线时仍可执行 ────────────────
    local_only_id = f"{skill_id}-local"
    try:
        device.upload_skill(user_a, _skill_zip(local_only_id, local_marker, "local"))
        listing = device.json(user_a, "GET", "/v1/desktop/capabilities/installations?kind=skill")
        local_only = next(
            (i for i in listing["items"] if i["runtime_name"] == local_only_id), None
        )
        report.step(
            "07 local-only skill coexists",
            bool(local_only) and local_only["source"] == "local" and local_only["usable"],
            source=(local_only or {}).get("source"),
            usable=(local_only or {}).get("usable"),
        )
    except Exception as exc:  # noqa: BLE001
        report.step("07 local-only skill coexists", False, error=str(exc)[:500])

    # ── 08 切换账号 ─────────────────────────────────────────────────────
    try:
        tok_b = cloud.token(user_b)
        device.push_bridge(cloud_internal, tok_b["token"], int(tok_b.get("expires_in") or 3600))
        device.json(user_b, "POST", "/v1/desktop/capabilities/sync")
        listing_b = device.json(user_b, "GET", "/v1/desktop/capabilities/installations?kind=skill")
        profile_b = listing_b["profile_id"]
        b_sees_a_private = any(
            i["runtime_name"] == cloud_skill_id and i["source"] == "cloud"
            for i in listing_b["items"]
        )
        a_files_kept = (device_root / "skills" / profile / cloud_skill_id).is_dir()
        local_user_b = str(device.me(user_b)["user_id"])
        view_b = _find_user_view(device_home, cloud_skill_id, local_user_b)
        report.step(
            "08 account switch isolation",
            profile_b != profile and not b_sees_a_private and a_files_kept and view_b is None,
            profile_a=profile,
            profile_b=profile_b,
            b_sees_a_private=b_sees_a_private,
            a_files_kept=a_files_kept,
            b_view_has_skill=_read_view_marker(view_b, device_home) is not None,
        )
        device.push_bridge(cloud_internal, token_a, int(tok.get("expires_in") or 3600))
        device.json(user_a, "POST", "/v1/desktop/capabilities/sync")
    except Exception as exc:  # noqa: BLE001
        report.step("08 account switch isolation", False, error=str(exc)[:500])

    # ── 09 相同云实例断网：本机副本继续，云操作明确失败 ─────────────────
    try:
        subprocess.run(
            ["docker", "stop", "-t", "2", args.cloud_container], check=True, capture_output=True
        )
        r = device.call(user_a, "POST", "/v1/desktop/capabilities/sync")
        listing = device.json(user_a, "GET", "/v1/desktop/capabilities/installations?kind=skill")
        local_ready = any(
            i["runtime_name"] == local_only_id and i["source"] == "local" and i["usable"]
            for i in listing["items"]
        )
        local_executed = _execute_local_marker(local_user_a, local_only_id, local_marker)
        report.step(
            "09 cloud unreachable",
            r.status_code == 502 and local_ready and local_executed,
            sync_status=r.status_code,
            local_ready=local_ready,
            local_execution_passed=local_executed,
        )
    except Exception as exc:
        report.step("09 cloud unreachable", False, error=str(exc)[:500])
    finally:
        subprocess.run(["docker", "start", args.cloud_container], check=True, capture_output=True)

    report.flush()
    failed = [s["step"] for s in report.steps if not s["ok"]]
    print(f"\nreport: {report.path}  failed: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
