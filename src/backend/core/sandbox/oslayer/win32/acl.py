"""Granting a capability SID write access on a path.

The restricted token only says *which* SIDs may write; the filesystem still has
to name one of them. This module adds the inheritable grant ACE that pairs with
the token, which is the same split Codex uses in
``codex-rs/windows-sandbox-rs/src/acl.rs`` (Apache-2.0).

Writing the ACE is idempotent by construction: ``SetEntriesInAclW`` replaces any
existing entry for the same trustee rather than appending a duplicate, so the
ACL converges instead of growing.

It is still checked before being written, and that check is not an optimisation.
``SetNamedSecurityInfoW`` propagates an inheritable ACE to every existing child
of the directory, so re-applying an ACE that is already there means walking the
whole workspace on every single command — a cost that grows with the workspace
until commands take minutes. Reading the ACL first is what keeps the grant a
once-per-folder event.
"""

from __future__ import annotations

import ctypes

from . import ffi
from .sid import OwnedSid


def grant_write_access(path: str, capability_sid: str) -> None:
    """Give ``capability_sid`` full, inheritable access to ``path`` and below."""
    _apply_ace(path, capability_sid, ffi.GRANT_ACCESS)


def deny_write_access(path: str, capability_sid: str) -> None:
    """Take write access back from ``capability_sid`` for ``path`` and below.

    Used for the carve-outs inside a writable root — the read-only subpaths a
    policy names and the protected metadata directories. ``SetEntriesInAclW``
    puts deny entries ahead of grants in the ACL, so this beats the inheritable
    grant on the root without having to remove it.
    """
    _apply_ace(path, capability_sid, ffi.DENY_ACCESS)


def _apply_ace(path: str, capability_sid: str, access_mode: int) -> None:
    security_descriptor = ctypes.c_void_p()
    current_dacl = ctypes.c_void_p()
    ffi.check_status(
        ffi.GetNamedSecurityInfoW(
            path,
            ffi.SE_FILE_OBJECT,
            ffi.DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.byref(current_dacl),
            None,
            ctypes.byref(security_descriptor),
        ),
        "GetNamedSecurityInfoW",
    )
    new_dacl = ctypes.c_void_p()
    try:
        with OwnedSid.from_string(capability_sid) as sid:
            if _already_applied(current_dacl, sid.pointer, access_mode):
                return

            entry = ffi.EXPLICIT_ACCESS_W()
            entry.grfAccessPermissions = ffi.GENERIC_ALL
            entry.grfAccessMode = access_mode
            entry.grfInheritance = ffi.SUB_CONTAINERS_AND_OBJECTS_INHERIT
            entry.Trustee.pMultipleTrustee = None
            entry.Trustee.MultipleTrusteeOperation = ffi.NO_MULTIPLE_TRUSTEE
            entry.Trustee.TrusteeForm = ffi.TRUSTEE_IS_SID
            entry.Trustee.TrusteeType = ffi.TRUSTEE_IS_UNKNOWN
            entry.Trustee.ptstrName = sid.pointer

            ffi.check_status(
                ffi.SetEntriesInAclW(1, ctypes.byref(entry), current_dacl, ctypes.byref(new_dacl)),
                "SetEntriesInAclW",
            )
            ffi.check_status(
                ffi.SetNamedSecurityInfoW(
                    path,
                    ffi.SE_FILE_OBJECT,
                    ffi.DACL_SECURITY_INFORMATION,
                    None,
                    None,
                    new_dacl,
                    None,
                ),
                "SetNamedSecurityInfoW",
            )
    finally:
        if new_dacl:
            ffi.LocalFree(new_dacl)
        if security_descriptor:
            ffi.LocalFree(security_descriptor)


_ACE_TYPE_FOR_MODE = {
    ffi.GRANT_ACCESS: ffi.ACCESS_ALLOWED_ACE_TYPE,
    ffi.DENY_ACCESS: ffi.ACCESS_DENIED_ACE_TYPE,
}


def _already_applied(dacl: ctypes.c_void_p, sid: ctypes.c_void_p, access_mode: int) -> bool:
    """Whether the ACL already carries exactly the entry we are about to write.

    An ACE only counts as a match when it is the right type for the same SID,
    already carries both inheritance flags, and grants at least the access the
    new one would — anything narrower still has to be written.
    """
    if not dacl:
        return False

    information = ffi.ACL_SIZE_INFORMATION()
    if not ffi.GetAclInformation(
        dacl,
        ctypes.byref(information),
        ctypes.sizeof(information),
        ffi.ACL_SIZE_INFORMATION_CLASS,
    ):
        return False

    wanted_type = _ACE_TYPE_FOR_MODE[access_mode]
    for index in range(information.AceCount):
        entry = ctypes.c_void_p()
        if not ffi.GetAce(dacl, index, ctypes.byref(entry)):
            return False
        ace = ctypes.cast(entry, ctypes.POINTER(ffi.ACCESS_ACE)).contents
        if ace.Header.AceType != wanted_type:
            continue
        if ace.Header.AceFlags & ffi.SUB_CONTAINERS_AND_OBJECTS_INHERIT != (
            ffi.SUB_CONTAINERS_AND_OBJECTS_INHERIT
        ):
            continue
        if ace.Mask & ffi.FILE_ALL_ACCESS != ffi.FILE_ALL_ACCESS:
            continue
        ace_sid = ctypes.c_void_p(entry.value + ffi.ACE_SID_OFFSET)
        if ffi.EqualSid(ace_sid, sid):
            return True
    return False


__all__ = ["deny_write_access", "grant_write_access"]
