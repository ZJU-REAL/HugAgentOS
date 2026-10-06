"""Managed package workers and provider-specific private HTTP endpoints."""
import asyncio
import base64
import io
import json
import logging
import secrets
import zipfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import httpx
from .protocol import ProcessRequest

BOOTSTRAP = """
import json, sys, zipfile, tempfile, importlib, os
from pathlib import Path
config = json.load(sys.stdin)
input_path = None
if os.name == "nt":
    import ctypes, msvcrt
    buffer = ctypes.create_unicode_buffer(32768)
    resolve_input = ctypes.windll.kernel32.GetFinalPathNameByHandleW
    resolve_input.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    if resolve_input(msvcrt.get_osfhandle(0), buffer, len(buffer), 0):
        input_path = Path(buffer.value)
elif Path("/proc/self/fd/0").exists():
    input_path = Path(os.readlink("/proc/self/fd/0"))
sys.stdin.close()
if input_path and input_path.name == "stdin.json":
    try:
        input_path.unlink()
    except PermissionError:
        input_path.write_text("")
root = Path(tempfile.mkdtemp(prefix=".__interactive_"))
archive_path = Path(config.pop("archive"))
with zipfile.ZipFile(archive_path) as archive:
    archive.extractall(root)
archive_path.unlink()
sys.path.insert(0, str(root))
module, name = config.pop("callable").split(":")
try:
    getattr(importlib.import_module(module), name)(config)
finally:
    import shutil
    shutil.rmtree(root)
"""

async def endpoint(provider, chat_id, user_id, port):
    provider = getattr(provider, "_ordinary", provider)
    if provider.name == "script_runner":
        parts = urlsplit(provider._base_url)
        host = parts.hostname or "localhost"
        if ":" in host:
            host = "[" + host + "]"
        return urlunsplit((parts.scheme, f"{host}:{port}", "", "", "")), {}
    if provider.name == "opensandbox":
        async def resolve():
            session = await provider._get_or_create_session(chat_id, user_id=user_id)
            target = await session.sandbox.get_endpoint(port)
            url = target.endpoint
            url = url if "://" in url else "http://" + url
            headers = dict(getattr(target, "headers", {}) or {})
            connection = session.sandbox.connection_config
            # Older servers omit gateway authentication from endpoint metadata.
            # Forward it only to the configured control-plane origin.
            remote, control = urlsplit(url), urlsplit(connection.get_base_url())
            if (remote.scheme, remote.netloc) == (control.scheme, control.netloc):
                headers = {**connection.headers, **headers}
                if connection.get_api_key():
                    headers.setdefault("OPEN-SANDBOX-API-KEY", connection.get_api_key())
            return url, headers
        loop = provider._service_loop
        if loop and loop is not asyncio.get_running_loop():
            from .opensandbox_provider import _forward_to_loop
            return await _forward_to_loop(loop, resolve())
        return await resolve()
    if provider.name == "cube":
        async with await provider._get_session_lock(chat_id):
            sandbox = await provider._acquire_persistent(chat_id, user_id)
            return "https://" + sandbox.get_host(port), {}
    raise ValueError("interactive_runtime_not_supported")

async def launch(provider, installation, module, user_id, chat_id, config):
    runtime = module["resource"]
    source = (installation.package / runtime["entry"]).resolve()
    if not source.is_relative_to(installation.package.resolve()) or not source.is_dir():
        raise ValueError("runtime_package_missing")
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise ValueError("runtime_symlink_not_allowed")
            if path.is_file() and path.suffix == ".py":
                archive.write(path, path.relative_to(source).as_posix())
    if data.tell() > 1024 * 1024:
        raise ValueError("runtime_package_too_large")
    archive_name = ".__interactive_" + secrets.token_hex(12) + ".zip"
    body = {**config, "archive": archive_name, "callable": runtime["callable"]}
    from core.plugins.resources.confinement import launch_policy
    confinement = launch_policy(provider, user_id, chat_id)
    process = await provider.start_process(ProcessRequest(
        sandbox_launch=confinement, script_content=BOOTSTRAP, script_name="interactive.py", timeout=None,
        user_id=user_id, session_id=chat_id, params=body,
        input_files_b64={archive_name: base64.b64encode(data.getvalue()).decode()},
    ), yield_time_ms=1000)
    handle = process.get("session_id")
    stdout = process.get("output", process.get("stdout", ""))
    errors = process.get("stderr", "")
    try:
        for _ in range(30):
            for line in stdout.splitlines():
                try:
                    ready = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ready, dict) and ready.get("runtime_ready"):
                    url, headers = await endpoint(provider, chat_id, user_id, int(ready["port"]))
                    headers["X-Hugagent-Resource-Token"] = config["token"]
                    async with httpx.AsyncClient(timeout=5, headers=headers, trust_env=False) as client:
                        for attempt in range(30):
                            try:
                                response = await client.get(url + "/state")
                                response.raise_for_status()
                                return {"url": url, "headers": headers, "process_id": handle, "sandbox_id": await provider.current_sandbox_id(chat_id), "provider": provider.name}
                            except httpx.HTTPError:
                                if attempt == 29:
                                    raise ValueError("runtime_start_failed")
                                await asyncio.sleep(0.5)
            if not handle or process.get("exit_code") is not None:
                raise ValueError("runtime_process_failed")
            process = await provider.write_stdin(handle, sandbox_session_id=chat_id, user_id=user_id, yield_time_ms=1000)
            stdout += process.get("output", process.get("stdout", ""))
            errors += process.get("stderr", "")
        raise ValueError("runtime_start_timeout")
    except BaseException:
        logging.getLogger(__name__).warning("Package worker startup failed: %s", (stdout + errors)[-3000:].replace(config["token"], "[redacted]"))
        if handle:
            await provider.write_stdin(handle, sandbox_session_id=chat_id, user_id=user_id, chars="\x03", yield_time_ms=1000)
        raise
