"""Mount authorized object volumes into an existing sandbox mount namespace."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys

AT_FDCWD = -100
OPEN_TREE_CLONE = 1
MOVE_MOUNT_F_EMPTY_PATH = 4
MOUNT_ROOT = Path("/mounts")
SECRET_ROOT = Path("/run/secrets")
CONNECT_TIMEOUT_SECONDS = 25


def mount_volume(index, volume):
    source = MOUNT_ROOT / str(index)
    source.mkdir(mode=0o700, exist_ok=True)
    password = SECRET_ROOT / ("password-" + str(index))
    fd = os.open(password, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(volume["credentials"])
        command = ["s3fs", volume["source"], str(source), "-o",
                   "passwd_file=" + str(password), "-o", "url=" + volume["endpoint"]]
        for option in volume["options"]:
            command.extend(["-o", option])
        result = subprocess.run(command, capture_output=True, timeout=CONNECT_TIMEOUT_SECONDS)
        if result.returncode:
            raise RuntimeError("Object filesystem connection failed")
    finally:
        password.unlink(missing_ok=True)
    return source


def mount_api():
    libc = ctypes.CDLL(None, use_errno=True)
    signatures = {
        "open_tree": [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint],
        "move_mount": [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                       ctypes.c_char_p, ctypes.c_uint],
        "setns": [ctypes.c_int, ctypes.c_int],
    }
    for name, arguments in signatures.items():
        function = getattr(libc, name)
        function.argtypes, function.restype = arguments, ctypes.c_int
        function.errcheck = checked_result
    return libc


def checked_result(result, function, arguments):
    if result < 0:
        raise OSError(ctypes.get_errno(), function.__name__ + " failed")
    return result


def restore(spec):
    libc = mount_api()
    trees = []
    namespace = os.open("/proc/1/ns/mnt", os.O_RDONLY)
    try:
        original = os.open("/proc/self/ns/mnt", os.O_RDONLY)
        try:
            for index, volume in enumerate(spec):
                source = mount_volume(index, volume)
                tree = libc.open_tree(AT_FDCWD, os.fsencode(source),
                                      OPEN_TREE_CLONE | os.O_CLOEXEC)
                trees.append((tree, volume["target"]))
            libc.setns(namespace, 0)
            try:
                for tree, target in trees:
                    libc.move_mount(tree, b"", AT_FDCWD,
                                    os.fsencode("/proc/1/root" + target),
                                    MOVE_MOUNT_F_EMPTY_PATH)
            finally:
                libc.setns(original, 0)
        finally:
            os.close(original)
    finally:
        for tree, _ in trees:
            os.close(tree)
        os.close(namespace)


if __name__ == "__main__":
    spec = json.loads(sys.stdin.readline())
    restore(spec)
    print("Object mounts restored", flush=True)
