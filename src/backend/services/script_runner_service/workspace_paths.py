"""会话工作目录的命名规则，宿主工具与执行服务共用。

没有任何路径别名：模型拿到的就是真实路径。这里只回答"某个会话的工作目录叫什么"，
不做 I/O、不读配置、不做权限判定。
"""
import hashlib
import ntpath
import posixpath

def session_root(root: str, session_id: str) -> str:
    value = (session_id or "").strip()
    if not value or len(value) > 512:
        raise ValueError("invalid workspace session_id")
    pathmod = ntpath if ntpath.splitdrive(root)[0] else posixpath
    return pathmod.join(root, ".sessions", hashlib.sha256(value.encode("utf-8")).hexdigest())
