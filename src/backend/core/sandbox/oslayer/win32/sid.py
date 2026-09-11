"""SID helpers: owning them, reading them off a token, minting capability SIDs.

A "capability SID" here is what Codex calls one in
``codex-rs/windows-sandbox-rs/src/cap.rs``: a randomly generated SID that names
no real account. It exists only so a filesystem ACE can grant *it* write access
and the sandbox token can carry *it* as a restricting SID. Nothing else on the
machine has that SID, so the ACE is meaningful only inside the sandbox.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from . import ffi


class OwnedSid:
    """A SID allocated by Win32 that must be released with ``LocalFree``."""

    def __init__(self, pointer: ctypes.c_void_p) -> None:
        self._pointer: ctypes.c_void_p | None = pointer

    @classmethod
    def from_string(cls, value: str) -> "OwnedSid":
        pointer = ctypes.c_void_p()
        ffi.check(
            ffi.ConvertStringSidToSidW(value, ctypes.byref(pointer)), "ConvertStringSidToSidW"
        )
        return cls(pointer)

    @property
    def pointer(self) -> ctypes.c_void_p:
        if self._pointer is None:
            raise RuntimeError("SID 已释放")
        return self._pointer

    def close(self) -> None:
        if self._pointer is not None:
            ffi.LocalFree(self._pointer)
            self._pointer = None

    def __enter__(self) -> "OwnedSid":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class SidBuffer:
    """A SID copied into a Python-owned buffer, valid for the buffer's lifetime."""

    def __init__(self, buffer: ctypes.Array) -> None:
        self._buffer = buffer

    @property
    def pointer(self) -> ctypes.c_void_p:
        return ctypes.cast(self._buffer, ctypes.c_void_p)


def world_sid() -> SidBuffer:
    """The ``Everyone`` SID."""
    size = wintypes.DWORD(0)
    ffi.CreateWellKnownSid(ffi.WIN_WORLD_SID, None, None, ctypes.byref(size))
    buffer = ctypes.create_string_buffer(size.value)
    ffi.check(
        ffi.CreateWellKnownSid(ffi.WIN_WORLD_SID, None, buffer, ctypes.byref(size)),
        "CreateWellKnownSid",
    )
    return SidBuffer(buffer)


def _query_token(handle: wintypes.HANDLE, info_class: int) -> ctypes.Array:
    needed = wintypes.DWORD(0)
    ffi.GetTokenInformation(handle, info_class, None, 0, ctypes.byref(needed))
    if needed.value == 0:
        raise ffi.Win32Error("GetTokenInformation", ctypes.get_last_error())
    buffer = ctypes.create_string_buffer(needed.value)
    ffi.check(
        ffi.GetTokenInformation(handle, info_class, buffer, needed, ctypes.byref(needed)),
        "GetTokenInformation",
    )
    return buffer


def token_user_sid(handle: wintypes.HANDLE) -> SidBuffer:
    """The token's own user SID, copied into a Python-owned buffer."""
    buffer = _query_token(handle, ffi.TOKEN_USER_CLASS)
    token_user = ctypes.cast(buffer, ctypes.POINTER(ffi.TOKEN_USER)).contents
    return _copy_sid(token_user.User.Sid)


def token_logon_sid(handle: wintypes.HANDLE) -> SidBuffer:
    """The logon session SID on ``handle``, following the linked token if needed.

    A filtered (UAC) token does not always carry the logon SID directly; its
    linked elevated token does. Codex looks in both places, and so must we —
    without it the restricted token cannot be built at all.
    """
    found = _scan_groups_for_logon(handle)
    if found is not None:
        return found

    buffer = _query_token(handle, ffi.TOKEN_LINKED_TOKEN_CLASS)
    linked = ctypes.cast(buffer, ctypes.POINTER(ffi.TOKEN_LINKED_TOKEN)).contents
    if not linked.LinkedToken:
        raise ffi.Win32Error("GetTokenInformation(TokenLinkedToken)", ctypes.get_last_error())
    try:
        found = _scan_groups_for_logon(linked.LinkedToken)
    finally:
        ffi.CloseHandle(linked.LinkedToken)
    if found is None:
        raise RuntimeError("当前进程令牌上没有登录会话 SID，无法构建受限令牌")
    return found


def _scan_groups_for_logon(handle: wintypes.HANDLE) -> SidBuffer | None:
    try:
        buffer = _query_token(handle, ffi.TOKEN_GROUPS_CLASS)
    except ffi.Win32Error:
        return None
    count = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD)).contents.value
    # TOKEN_GROUPS is `DWORD GroupCount; SID_AND_ATTRIBUTES Groups[]`, and the
    # array starts at the structure's own alignment, not right after the DWORD.
    alignment = ctypes.alignment(ffi.SID_AND_ATTRIBUTES)
    offset = (ctypes.sizeof(wintypes.DWORD) + alignment - 1) & ~(alignment - 1)
    groups = ctypes.cast(ctypes.byref(buffer, offset), ctypes.POINTER(ffi.SID_AND_ATTRIBUTES))
    for index in range(count):
        entry = groups[index]
        if entry.Attributes & ffi.SE_GROUP_LOGON_ID == ffi.SE_GROUP_LOGON_ID:
            return _copy_sid(entry.Sid)
    return None


def _copy_sid(pointer: ctypes.c_void_p) -> SidBuffer:
    length = ffi.GetLengthSid(pointer)
    if length == 0:
        raise ffi.Win32Error("GetLengthSid", ctypes.get_last_error())
    buffer = ctypes.create_string_buffer(length)
    ffi.check(ffi.CopySid(length, buffer, pointer), "CopySid")
    return SidBuffer(buffer)


__all__ = [
    "OwnedSid",
    "SidBuffer",
    "token_logon_sid",
    "token_user_sid",
    "world_sid",
]
