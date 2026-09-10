"""Restricted-token construction, ported from Codex's ``token.rs`` (Apache-2.0).

The sandbox token is the current process token re-created with
``CreateRestrictedToken`` under three flags:

``WRITE_RESTRICTED``
    Write access checks are evaluated a second time against the token's
    *restricting* SID list. A file is writable only if its ACL grants one of
    those SIDs, which is how the writable roots — and only those — stay writable.
``DISABLE_MAX_PRIVILEGE``
    Every privilege is dropped. ``SeChangeNotifyPrivilege`` is put back
    afterwards because without it path traversal fails on almost every process.
``LUA_TOKEN``
    The token is filtered down to a standard-user integrity level.

The restricting list is, in Codex's order: the capability SIDs for this run's
writable roots, then the logon session SID, then ``Everyone``. The last two are
what keep ordinary processes working — pipes, shared temp objects and console
handles are granted to them — and they are also the honest limit of this
backend: anything whose ACL already grants ``Everyone`` write remains writable.
User data does not, which is what the sandbox is protecting.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from . import ffi
from .sid import OwnedSid, token_logon_sid, world_sid

CHANGE_NOTIFY_PRIVILEGE = "SeChangeNotifyPrivilege"


def open_current_process_token() -> wintypes.HANDLE:
    """Open this process's token with the rights needed to restrict it."""
    desired = (
        ffi.TOKEN_DUPLICATE
        | ffi.TOKEN_QUERY
        | ffi.TOKEN_ASSIGN_PRIMARY
        | ffi.TOKEN_ADJUST_DEFAULT
        | ffi.TOKEN_ADJUST_SESSIONID
        | ffi.TOKEN_ADJUST_PRIVILEGES
    )
    handle = wintypes.HANDLE()
    ffi.check(
        ffi.OpenProcessToken(ffi.GetCurrentProcess(), desired, ctypes.byref(handle)),
        "OpenProcessToken",
    )
    return handle


def create_restricted_token(
    base_token: wintypes.HANDLE, capability_sids: list[str]
) -> wintypes.HANDLE:
    """Build the sandbox token carrying ``capability_sids`` as restricting SIDs."""
    if not capability_sids:
        raise ValueError("受限令牌至少需要一个能力 SID")

    owned = [OwnedSid.from_string(value) for value in capability_sids]
    try:
        logon = token_logon_sid(base_token)
        everyone = world_sid()
        # Order matters and mirrors the upstream implementation: capabilities
        # first, then the identities that keep ordinary IPC working.
        pointers = [sid.pointer for sid in owned] + [logon.pointer, everyone.pointer]

        entries = (ffi.SID_AND_ATTRIBUTES * len(pointers))()
        for index, pointer in enumerate(pointers):
            entries[index].Sid = pointer
            entries[index].Attributes = 0

        token = wintypes.HANDLE()
        flags = ffi.DISABLE_MAX_PRIVILEGE | ffi.LUA_TOKEN | ffi.WRITE_RESTRICTED
        ffi.check(
            ffi.CreateRestrictedToken(
                base_token,
                flags,
                0,
                None,
                0,
                None,
                len(entries),
                entries,
                ctypes.byref(token),
            ),
            "CreateRestrictedToken",
        )
        _set_permissive_default_dacl(token, pointers)
        _enable_privilege(token, CHANGE_NOTIFY_PRIVILEGE)
        return token
    finally:
        for sid in owned:
            sid.close()


def _set_permissive_default_dacl(token: wintypes.HANDLE, sids: list[ctypes.c_void_p]) -> None:
    """Let the sandboxed process create its own pipes and IPC objects.

    Without this the token's default DACL excludes its own restricting SIDs and
    anything that builds a pipeline — a shell above all — fails with access
    denied on objects it created itself.
    """
    entries = (ffi.EXPLICIT_ACCESS_W * len(sids))()
    for index, pointer in enumerate(sids):
        entries[index].grfAccessPermissions = ffi.GENERIC_ALL
        entries[index].grfAccessMode = ffi.GRANT_ACCESS
        entries[index].grfInheritance = ffi.NO_INHERITANCE
        entries[index].Trustee.pMultipleTrustee = None
        entries[index].Trustee.MultipleTrusteeOperation = ffi.NO_MULTIPLE_TRUSTEE
        entries[index].Trustee.TrusteeForm = ffi.TRUSTEE_IS_SID
        entries[index].Trustee.TrusteeType = ffi.TRUSTEE_IS_UNKNOWN
        entries[index].Trustee.ptstrName = pointer

    new_dacl = ctypes.c_void_p()
    ffi.check_status(
        ffi.SetEntriesInAclW(len(entries), entries, None, ctypes.byref(new_dacl)),
        "SetEntriesInAclW",
    )
    try:
        info = ffi.TOKEN_DEFAULT_DACL(DefaultDacl=new_dacl)
        ffi.check(
            ffi.SetTokenInformation(
                token,
                ffi.TOKEN_DEFAULT_DACL_CLASS,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ),
            "SetTokenInformation(TokenDefaultDacl)",
        )
    finally:
        ffi.LocalFree(new_dacl)


def _enable_privilege(token: wintypes.HANDLE, name: str) -> None:
    luid = ffi.LUID()
    ffi.check(ffi.LookupPrivilegeValueW(None, name, ctypes.byref(luid)), "LookupPrivilegeValueW")
    privileges = ffi.TOKEN_PRIVILEGES()
    privileges.PrivilegeCount = 1
    privileges.Privileges[0].Luid = luid
    privileges.Privileges[0].Attributes = ffi.SE_PRIVILEGE_ENABLED
    ctypes.set_last_error(0)
    ffi.check(
        ffi.AdjustTokenPrivileges(token, False, ctypes.byref(privileges), 0, None, None),
        "AdjustTokenPrivileges",
    )
    code = ctypes.get_last_error()
    if code != ffi.ERROR_SUCCESS:
        raise ffi.Win32Error("AdjustTokenPrivileges", code)


__all__ = ["CHANGE_NOTIFY_PRIVILEGE", "create_restricted_token", "open_current_process_token"]
