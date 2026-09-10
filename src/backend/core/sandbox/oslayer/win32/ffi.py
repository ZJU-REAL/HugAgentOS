"""Raw Win32 bindings used by the Windows sandbox backend.

Only the declarations the sandbox needs, and nothing above them: every function
here maps one-to-one onto the API of the same name so the layers above read like
the Win32 documentation they were written from. Importing this module on a
non-Windows host raises immediately — there is no stub to accidentally run
against.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

if sys.platform != "win32":  # pragma: no cover - guarded at import site
    raise ImportError("core.sandbox.oslayer.win32 只能在 Windows 上导入")

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ── Token access rights and creation flags ───────────────────────────────────
TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_ADJUST_DEFAULT = 0x0080
TOKEN_ADJUST_SESSIONID = 0x0100

DISABLE_MAX_PRIVILEGE = 0x01
LUA_TOKEN = 0x04
WRITE_RESTRICTED = 0x08

TOKEN_USER_CLASS = 1
TOKEN_GROUPS_CLASS = 2
TOKEN_DEFAULT_DACL_CLASS = 6
TOKEN_LINKED_TOKEN_CLASS = 19

SE_GROUP_LOGON_ID = 0xC0000000
SE_PRIVILEGE_ENABLED = 0x00000002
WIN_WORLD_SID = 1

# ── Access control ───────────────────────────────────────────────────────────
GRANT_ACCESS = 1
DENY_ACCESS = 3
TRUSTEE_IS_SID = 0
TRUSTEE_IS_UNKNOWN = 0
NO_MULTIPLE_TRUSTEE = 0
NO_INHERITANCE = 0x0
OBJECT_INHERIT_ACE = 0x1
CONTAINER_INHERIT_ACE = 0x2
SUB_CONTAINERS_AND_OBJECTS_INHERIT = OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE

GENERIC_ALL = 0x10000000
# What Windows stores after mapping GENERIC_ALL onto a file object. An ACE read
# back from disk carries this, never the generic form, so it is what an
# already-applied grant has to be compared against.
FILE_ALL_ACCESS = 0x001F01FF
SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x00000004

ACCESS_ALLOWED_ACE_TYPE = 0x0
ACCESS_DENIED_ACE_TYPE = 0x1
# Offset of the SID inside ACCESS_ALLOWED_ACE / ACCESS_DENIED_ACE: a 4-byte
# ACE_HEADER followed by a 4-byte ACCESS_MASK.
ACE_SID_OFFSET = 8
ACL_SIZE_INFORMATION_CLASS = 2

ERROR_SUCCESS = 0

# ── Process creation ─────────────────────────────────────────────────────────
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
STARTF_USESTDHANDLES = 0x00000100
STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
STD_ERROR_HANDLE = -12
HANDLE_FLAG_INHERIT = 0x00000001
INFINITE = 0xFFFFFFFF


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", SID_AND_ATTRIBUTES)]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]


class TRUSTEE_W(ctypes.Structure):
    _fields_ = [
        ("pMultipleTrustee", ctypes.c_void_p),
        ("MultipleTrusteeOperation", ctypes.c_int),
        ("TrusteeForm", ctypes.c_int),
        ("TrusteeType", ctypes.c_int),
        ("ptstrName", ctypes.c_void_p),
    ]


class EXPLICIT_ACCESS_W(ctypes.Structure):
    _fields_ = [
        ("grfAccessPermissions", wintypes.DWORD),
        ("grfAccessMode", ctypes.c_int),
        ("grfInheritance", wintypes.DWORD),
        ("Trustee", TRUSTEE_W),
    ]


class ACL_SIZE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    ]


class ACE_HEADER(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", wintypes.WORD),
    ]


class ACCESS_ACE(ctypes.Structure):
    """Common prefix of ACCESS_ALLOWED_ACE and ACCESS_DENIED_ACE."""

    _fields_ = [("Header", ACE_HEADER), ("Mask", wintypes.DWORD)]


class TOKEN_DEFAULT_DACL(ctypes.Structure):
    _fields_ = [("DefaultDacl", ctypes.c_void_p)]


class TOKEN_LINKED_TOKEN(ctypes.Structure):
    _fields_ = [("LinkedToken", wintypes.HANDLE)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


OpenProcessToken = advapi32.OpenProcessToken
OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
OpenProcessToken.restype = wintypes.BOOL

GetTokenInformation = advapi32.GetTokenInformation
GetTokenInformation.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
GetTokenInformation.restype = wintypes.BOOL

SetTokenInformation = advapi32.SetTokenInformation
SetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
SetTokenInformation.restype = wintypes.BOOL

CreateRestrictedToken = advapi32.CreateRestrictedToken
CreateRestrictedToken.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(SID_AND_ATTRIBUTES),
    ctypes.POINTER(wintypes.HANDLE),
]
CreateRestrictedToken.restype = wintypes.BOOL

CreateWellKnownSid = advapi32.CreateWellKnownSid
CreateWellKnownSid.argtypes = [
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.POINTER(wintypes.DWORD),
]
CreateWellKnownSid.restype = wintypes.BOOL

ConvertStringSidToSidW = advapi32.ConvertStringSidToSidW
ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
ConvertStringSidToSidW.restype = wintypes.BOOL

GetLengthSid = advapi32.GetLengthSid
GetLengthSid.argtypes = [ctypes.c_void_p]
GetLengthSid.restype = wintypes.DWORD

CopySid = advapi32.CopySid
CopySid.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
CopySid.restype = wintypes.BOOL

LookupPrivilegeValueW = advapi32.LookupPrivilegeValueW
LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)]
LookupPrivilegeValueW.restype = wintypes.BOOL

AdjustTokenPrivileges = advapi32.AdjustTokenPrivileges
AdjustTokenPrivileges.argtypes = [
    wintypes.HANDLE,
    wintypes.BOOL,
    ctypes.POINTER(TOKEN_PRIVILEGES),
    wintypes.DWORD,
    ctypes.c_void_p,
    ctypes.c_void_p,
]
AdjustTokenPrivileges.restype = wintypes.BOOL

SetEntriesInAclW = advapi32.SetEntriesInAclW
SetEntriesInAclW.argtypes = [
    wintypes.ULONG,
    ctypes.POINTER(EXPLICIT_ACCESS_W),
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p),
]
SetEntriesInAclW.restype = wintypes.DWORD

GetNamedSecurityInfoW = advapi32.GetNamedSecurityInfoW
GetNamedSecurityInfoW.argtypes = [
    wintypes.LPCWSTR,
    ctypes.c_int,
    wintypes.DWORD,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p),
]
GetNamedSecurityInfoW.restype = wintypes.DWORD

SetNamedSecurityInfoW = advapi32.SetNamedSecurityInfoW
SetNamedSecurityInfoW.argtypes = [
    wintypes.LPWSTR,
    ctypes.c_int,
    wintypes.DWORD,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
]
SetNamedSecurityInfoW.restype = wintypes.DWORD

CreateProcessAsUserW = advapi32.CreateProcessAsUserW
CreateProcessAsUserW.argtypes = [
    wintypes.HANDLE,
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.BOOL,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.LPCWSTR,
    ctypes.POINTER(STARTUPINFOW),
    ctypes.POINTER(PROCESS_INFORMATION),
]
CreateProcessAsUserW.restype = wintypes.BOOL

GetAclInformation = advapi32.GetAclInformation
GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int]
GetAclInformation.restype = wintypes.BOOL

GetAce = advapi32.GetAce
GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
GetAce.restype = wintypes.BOOL

EqualSid = advapi32.EqualSid
EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
EqualSid.restype = wintypes.BOOL

GetCurrentProcess = kernel32.GetCurrentProcess
GetCurrentProcess.argtypes = []
GetCurrentProcess.restype = wintypes.HANDLE

CloseHandle = kernel32.CloseHandle
CloseHandle.argtypes = [wintypes.HANDLE]
CloseHandle.restype = wintypes.BOOL

LocalFree = kernel32.LocalFree
LocalFree.argtypes = [ctypes.c_void_p]
LocalFree.restype = ctypes.c_void_p

GetStdHandle = kernel32.GetStdHandle
GetStdHandle.argtypes = [wintypes.DWORD]
GetStdHandle.restype = wintypes.HANDLE

SetHandleInformation = kernel32.SetHandleInformation
SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
SetHandleInformation.restype = wintypes.BOOL

WaitForSingleObject = kernel32.WaitForSingleObject
WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
WaitForSingleObject.restype = wintypes.DWORD

GetExitCodeProcess = kernel32.GetExitCodeProcess
GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
GetExitCodeProcess.restype = wintypes.BOOL

TerminateProcess = kernel32.TerminateProcess
TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
TerminateProcess.restype = wintypes.BOOL


class Win32Error(OSError):
    """A Win32 call failed; carries the API name and ``GetLastError`` code."""

    def __init__(self, api: str, code: int) -> None:
        super().__init__(f"{api} 调用失败（Win32 错误码 {code}）")
        self.api = api
        self.code = code


def check(result: object, api: str) -> None:
    """Raise :class:`Win32Error` when a BOOL-returning API reported failure."""
    if not result:
        raise Win32Error(api, ctypes.get_last_error())


def check_status(status: int, api: str) -> None:
    """Raise :class:`Win32Error` when a status-returning API did not succeed."""
    if status != ERROR_SUCCESS:
        raise Win32Error(api, status)
