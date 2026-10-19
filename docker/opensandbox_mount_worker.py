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
MOVE_MOUNT_T_EMPTY_PATH = 64
MNT_DETACH = 2
MOUNT_ROOT = Path("/mounts")
SECRET_ROOT = Path("/run/secrets")
SANDBOX_ROOT = Path("/proc/1/root")
MOUNTINFO = Path("/proc/1/mountinfo")
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
                   "passwd_file=" + str(password), "-o", "url=" + volume["endpoint"],
                   "-o", "ensure_diskfree=2048"]
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
        "umount2": [ctypes.c_char_p, ctypes.c_int],
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


def target_directory(target):
    """Resolve only real directories beneath the sandbox root, without symlinks."""
    parts = Path(target).parts
    if not target.startswith("/") or ".." in parts or target == "/":
        raise ValueError("Invalid object mount target")
    fd = os.open(SANDBOX_ROOT, os.O_PATH | os.O_DIRECTORY)
    try:
        for part in parts[1:]:
            try:
                os.mkdir(part, mode=0o755, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def detach_previous(libc, target, destination):
    # Pin the authorized mount before inspecting it. umount uses this trusted
    # procfs descriptor, never a sandbox-controlled path that can be renamed.
    descriptor = "/proc/self/fd/" + str(destination)
    details = Path("/proc/self/fdinfo/" + str(destination)).read_text().splitlines()
    mount_id = next(line.split()[1] for line in details if line.startswith("mnt_id:"))
    own_mount = None
    for line in MOUNTINFO.read_text().splitlines():
        fields = line.split()
        if fields[0] == mount_id:
            own_mount = fields
        if fields[4] == target:
            if fields[0] != mount_id:
                raise RuntimeError("Object mount target changed during recovery")
            if not fields[fields.index("-") + 1].startswith("fuse"):
                raise RuntimeError("Object target is occupied by another filesystem")
            libc.umount2(os.fsencode(descriptor), MNT_DETACH)
            return True
    if own_mount and own_mount[own_mount.index("-") + 1].startswith("fuse"):
        raise RuntimeError("Object mount moved outside its authorized target")
    return False


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
                    destination = target_directory(target)
                    try:
                        if detach_previous(libc, target, destination):
                            os.close(destination)
                            destination = None
                            destination = target_directory(target)
                        libc.move_mount(tree, b"", destination, b"",
                            MOVE_MOUNT_F_EMPTY_PATH | MOVE_MOUNT_T_EMPTY_PATH)
                    finally:
                        if destination is not None:
                            os.close(destination)
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
    print("Object mounts ready", flush=True)
