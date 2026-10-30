"""Supervise S3FS. An absent mount never becomes a writable fallback."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

def mounted(path):
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, after = line.split(" - ", 1)
        if before.split()[4] == path and after.split()[0] == "fuse.s3fs":
            return True
    return False

def prepare_mount(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o755)
    # A killed FUSE process leaves a disconnected mount. Detach it
    # before stat/mkdir, which would otherwise fail with ENOTCONN.
    if mounted(str(path)):
        subprocess.run(["fusermount", "-u", str(path)], check=True)
    path.mkdir(exist_ok=True)
    path.chmod(0)

def main():
    specs = json.loads(os.environ["SPACE_MOUNTS"])
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        return 0 if all(mounted(s["path"]) for s in specs) else 1
    credential = Path("/run/s3fs.passwd")
    access = Path("/run/space-credentials/access").read_text().strip()
    secret = Path("/run/space-credentials/secret").read_text().strip()
    if any(c in access + secret for c in "\r\n:"):
        raise ValueError("Unsupported S3 credential format")
    fd = os.open(credential, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(access + ":" + secret)
    children = []
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for spec in specs:
            path = Path(spec["path"])
            prepare_mount(path)
            options = ["passwd_file=" + str(credential), "url=" + os.environ["S3_ENDPOINT"],
                       "endpoint=" + os.environ["S3_REGION"], "use_path_request_style", "sigv4",
                       "allow_other", "uid=1000", "gid=1000", "umask=0022",
                       "stat_cache_expire=0", "attr_timeout=0", "entry_timeout=0",
                       "negative_timeout=0", "nosuid", "nodev"]
            if spec["read_only"]:
                options.append("ro")
            command = ["s3fs", os.environ["S3_BUCKET"] + ":/" + spec["prefix"], str(path), "-f"]
            for option in options:
                command.extend(["-o", option])
            children.append(subprocess.Popen(command))
        while not stopping:
            if any(child.poll() is not None for child in children):
                raise RuntimeError("Object filesystem exited")
            time.sleep(0.5)
        return 0
    finally:
        for child in children:
            child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        credential.unlink(missing_ok=True)

if __name__ == "__main__":
    sys.exit(main())
