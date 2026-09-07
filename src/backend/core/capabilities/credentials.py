"""Device credential store for ``credentialRef`` values in ``mcp.json``.

Secrets never live in the capability directory or the business database. They
are handed to the operating system's credential store:

- Windows: Credential Manager (``advapi32`` ``CredWriteW`` / ``CredReadW`` / ``CredDeleteW``)
- macOS: the login Keychain via the ``security`` CLI
- Linux: the Secret Service via ``secret-tool`` when present

When no store is available the operation fails loudly; there is deliberately
no plaintext-file substitute.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Dict, Optional

SERVICE = "cn.hugagent.agent.desktop.mcp"
REF_PREFIX = "os-store:"


class CredentialStoreUnavailable(RuntimeError):
    pass


def make_ref(name: str) -> str:
    return f"{REF_PREFIX}{name}"


def parse_ref(ref: str) -> str:
    if not ref.startswith(REF_PREFIX) or len(ref) <= len(REF_PREFIX):
        raise ValueError(f"unsupported credentialRef {ref!r}")
    return ref[len(REF_PREFIX) :]


# ── Windows ─────────────────────────────────────────────────────────────


def _win_target(name: str) -> str:
    return f"{SERVICE}/{name}"


def _win_write(name: str, secret: str) -> None:
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    blob = secret.encode("utf-16-le")
    cred = CREDENTIAL()
    cred.Type = 1  # CRED_TYPE_GENERIC
    cred.TargetName = _win_target(name)
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(
        ctypes.create_string_buffer(blob, len(blob)), ctypes.POINTER(ctypes.c_char)
    )
    cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE (this user, this machine)
    cred.UserName = SERVICE
    advapi = ctypes.windll.advapi32
    if not advapi.CredWriteW(ctypes.byref(cred), 0):
        raise CredentialStoreUnavailable(f"CredWriteW failed: {ctypes.GetLastError()}")


def _win_read(name: str) -> Optional[str]:
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.windll.advapi32
    pcred = ctypes.c_void_p()
    if not advapi.CredReadW(_win_target(name), 1, 0, ctypes.byref(pcred)):
        err = ctypes.GetLastError()
        if err == 1168:  # ERROR_NOT_FOUND
            return None
        raise CredentialStoreUnavailable(f"CredReadW failed: {err}")
    try:
        # Offsets: Flags(4) Type(4) TargetName(ptr) Comment(ptr) LastWritten(8) BlobSize(4) Blob(ptr)
        ptr_size = ctypes.sizeof(ctypes.c_void_p)
        base = pcred.value
        size_off = 8 + 2 * ptr_size + 8
        size = wintypes.DWORD.from_address(base + size_off).value
        blob_off = size_off + 4
        blob_off += (ptr_size - (blob_off % ptr_size)) % ptr_size
        blob_ptr = ctypes.c_void_p.from_address(base + blob_off).value
        data = ctypes.string_at(blob_ptr, size)
        return data.decode("utf-16-le")
    finally:
        advapi.CredFree(pcred)


def _win_delete(name: str) -> bool:
    import ctypes

    advapi = ctypes.windll.advapi32
    if advapi.CredDeleteW(_win_target(name), 1, 0):
        return True
    return ctypes.GetLastError() != 1168 and False


# ── macOS / Linux ───────────────────────────────────────────────────────


def _run(cmd: list, *, input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=input_text, capture_output=True, text=True, check=False)


def _mac_write(name: str, secret: str) -> None:
    proc = _run(["security", "add-generic-password", "-U", "-s", SERVICE, "-a", name, "-w", secret])
    if proc.returncode != 0:
        raise CredentialStoreUnavailable(
            f"security add-generic-password failed: {proc.stderr.strip()}"
        )


def _mac_read(name: str) -> Optional[str]:
    proc = _run(["security", "find-generic-password", "-s", SERVICE, "-a", name, "-w"])
    if proc.returncode == 44:
        return None
    if proc.returncode != 0:
        raise CredentialStoreUnavailable(
            f"security find-generic-password failed: {proc.stderr.strip()}"
        )
    return proc.stdout.rstrip("\n")


def _mac_delete(name: str) -> bool:
    proc = _run(["security", "delete-generic-password", "-s", SERVICE, "-a", name])
    return proc.returncode == 0


def _linux_tool() -> str:
    tool = shutil.which("secret-tool")
    if not tool:
        raise CredentialStoreUnavailable("secret-tool (libsecret) is not installed")
    return tool


def _linux_write(name: str, secret: str) -> None:
    proc = _run(
        [
            _linux_tool(),
            "store",
            "--label",
            f"{SERVICE}/{name}",
            "service",
            SERVICE,
            "account",
            name,
        ],
        input_text=secret,
    )
    if proc.returncode != 0:
        raise CredentialStoreUnavailable(f"secret-tool store failed: {proc.stderr.strip()}")


def _linux_read(name: str) -> Optional[str]:
    proc = _run([_linux_tool(), "lookup", "service", SERVICE, "account", name])
    if proc.returncode != 0:
        return None
    return proc.stdout


def _linux_delete(name: str) -> bool:
    proc = _run([_linux_tool(), "clear", "service", SERVICE, "account", name])
    return proc.returncode == 0


# ── public API ──────────────────────────────────────────────────────────


def _backend():
    override = os.getenv("HUGAGENT_CREDENTIAL_BACKEND", "").strip()
    if override == "memory":  # tests only
        return _MEMORY
    if sys.platform == "win32":
        return (_win_write, _win_read, _win_delete)
    if sys.platform == "darwin":
        return (_mac_write, _mac_read, _mac_delete)
    return (_linux_write, _linux_read, _linux_delete)


_memory_store: Dict[str, str] = {}
_MEMORY = (
    lambda n, s: _memory_store.__setitem__(n, s),
    lambda n: _memory_store.get(n),
    lambda n: _memory_store.pop(n, None) is not None,
)


def store_secret(name: str, secret: str) -> str:
    """Persist a secret under ``name``; returns the credentialRef to put in mcp.json."""
    write, _read, _delete = _backend()
    write(name, secret)
    return make_ref(name)


def load_secret(ref: str) -> Optional[str]:
    _write, read, _delete = _backend()
    return read(parse_ref(ref))


def delete_secret(ref: str) -> bool:
    _write, _read, delete = _backend()
    return bool(delete(parse_ref(ref)))


def store_headers(name: str, headers: Dict[str, str]) -> str:
    return store_secret(name, json.dumps(headers, ensure_ascii=False))


def load_headers(ref: str) -> Dict[str, str]:
    raw = load_secret(ref)
    if raw is None:
        raise CredentialStoreUnavailable(f"credential {ref} is missing from the device store")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise CredentialStoreUnavailable(f"credential {ref} is not a header map")
    return {str(k): str(v) for k, v in value.items()}
