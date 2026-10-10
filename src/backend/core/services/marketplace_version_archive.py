"""Bounded, credential-free packages for marketplace detail updates."""

import io
import json
import re
import stat
import zipfile
from pathlib import PurePosixPath
from urllib.parse import parse_qsl, urlsplit

from fastapi import HTTPException

MAX_ZIP = 20 * 1024 * 1024
MAX_EXPANDED = 50 * 1024 * 1024
MAX_FILES = 1000
MAX_FILE = 10 * 1024 * 1024
SECRET_KEY = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|cookie|credential|private[_-]?key)"
)


def fail(message):
    raise HTTPException(422, message)


def next_version(value, bump):
    match = re.fullmatch(r"[Vv]?(\d+)(?:\.(\d+))?(?:\.(\d+))?", value or "1.0.0")
    parts = [int(x or 0) for x in match.groups()] if match else [1, 0, 0]
    if bump not in {"major", "minor", "patch"}:
        fail("无效的版本增量")
    index = {"major": 0, "minor": 1, "patch": 2}[bump]
    parts[index] += 1
    for n in range(index + 1, 3):
        parts[n] = 0
    return ".".join(map(str, parts))


def check_url(value):
    parsed = urlsplit(value or "")
    if parsed.username or parsed.password:
        fail("URL 不能包含凭据")
    check_secrets(dict(parse_qsl(parsed.query)))


def check_text(value):
    # Detect common credential-bearing CLI flags, assignments and YAML values.
    keys = r"(?:api[_-]?key|access[_-]?token|token|password|passwd|secret|authorization)"
    pattern = r"(?i)(?:--" + keys + r"\s+|(?:--)?" + keys + r"\s*[=:]\s*)['\"]?([^\s'\"]+)"
    for match in re.finditer(pattern, value):
        literal = match.group(1)
        if literal and not literal.startswith(
            ("$", "<", "YOUR_", "your_", "{", "***", "os.getenv", "os.environ", "process.env")
        ):
            fail("配置或脚本不能包含内联凭据")
    if "-----BEGIN " in value and "PRIVATE KEY-----" in value:
        fail("包内不能包含私钥")


def check_secrets(value):
    """Reject literal credentials; placeholders are declarations, not credentials."""
    if isinstance(value, dict):
        for key, child in value.items():
            if SECRET_KEY.search(str(key)) and isinstance(child, str) and child:
                if not re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", child):
                    fail("包内不能包含连接凭据，请使用专门的连接配置入口")
            check_secrets(child)
    elif isinstance(value, str):
        check_text(value)
        if value.startswith(("http://", "https://")):
            check_url(value)
    elif isinstance(value, list):
        if all(isinstance(child, str) for child in value):
            check_text(" ".join(value))
        for child in value:
            check_secrets(child)


def read_zip(raw):
    if not raw or len(raw) > MAX_ZIP:
        fail("ZIP 必须非空且不超过 20 MiB")
    files, targets, total = {}, set(), 0
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES:
                fail("ZIP 条目数量不能超过 1000")
            for entry in entries:
                name = entry.filename.replace("\\", "/")
                path = PurePosixPath(name)
                mode = entry.external_attr >> 16
                if (
                    not name
                    or not path.parts
                    or path.is_absolute()
                    or ".." in path.parts
                    or any(":" in p or "\x00" in p for p in path.parts)
                    or stat.S_ISLNK(mode)
                    or entry.flag_bits & 1
                    or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))
                ):
                    fail("ZIP 包含非法路径、链接或加密条目")
                key = path.as_posix().casefold().rstrip("/")
                if key in targets:
                    fail("ZIP 包含重复路径")
                targets.add(key)
                if any(p in {".git", "node_modules", "__pycache__"} for p in path.parts):
                    fail("ZIP 不能包含 .git、node_modules 或缓存目录")
                basename = path.name.lower()
                if (
                    basename.startswith(".env")
                    or basename in {"secrets.json", "credentials.json"}
                    or basename.endswith((".key", ".pem", ".p12", ".pfx"))
                ):
                    fail("ZIP 不能包含凭据文件")
                if entry.is_dir():
                    continue
                total += entry.file_size
                if entry.file_size > MAX_FILE or total > MAX_EXPANDED:
                    fail("单文件不能超过 10 MiB，解压总量不能超过 50 MiB")
                if entry.file_size > max(entry.compress_size, 1) * 200:
                    fail("ZIP 压缩比异常")
                data = archive.read(entry)
                if len(data) != entry.file_size:
                    fail("ZIP 文件长度校验失败")
                if name.lower().endswith(".json"):
                    try:
                        check_secrets(json.loads(data))
                    except (ValueError, UnicodeError):
                        fail("包内 JSON 文件格式无效")
                if b"-----BEGIN " in data and b"PRIVATE KEY-----" in data:
                    fail("ZIP 不能包含私钥")
                suffix = path.suffix.lower()
                if suffix:
                    if suffix in {".py", ".js", ".ts", ".sh", ".yaml", ".yml", ".toml", ".ini"}:
                        try:
                            check_text(data.decode("utf-8"))
                        except UnicodeError:
                            fail("脚本或配置必须使用 UTF-8")
                files[path.as_posix()] = data
    except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError):
        fail("无效或不受支持的 ZIP")
    for name in files:
        for parent in PurePosixPath(name).parents:
            if parent.as_posix() in files:
                fail("ZIP 文件路径与目录冲突")
    if not files:
        fail("ZIP 内没有文件")
    return files


def strip_wrapper(files, kind):
    markers = (
        {"SKILL.md"}
        if kind == "skill"
        else {"plugin.json", ".claude-plugin/plugin.json", ".codex-plugin/plugin.json"}
    )
    roots = set()
    for name in files:
        matching = [marker for marker in markers if name == marker or name.endswith("/" + marker)]
        if matching:
            marker = max(matching, key=len)
            roots.add(name[: -len(marker)])
    if kind == "plugin":
        # Nested plugin packages are ambiguous and cannot silently discard files.
        roots = {root for root in roots if not root.startswith("skills/")}
    if len(roots) != 1:
        fail("包中必须恰好包含一个资源根目录")
    prefix = roots.pop()
    if any(not name.startswith(prefix) for name in files):
        fail("包内存在资源根目录之外的文件")
    return {name[len(prefix) :]: data for name, data in files.items()}


def skill_version(content, version):
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    if not content.startswith("---\n") or "\n---\n" not in content[4:]:
        fail("SKILL.md 缺少有效 frontmatter")
    end = content.index("\n---\n", 4)
    front = content[4:end]
    front = re.sub(r"^version:.*(?:\n|$)", "", front, flags=re.M)
    return "---\n" + front.rstrip() + "\nversion: " + version + content[end:]


def diff_files(before, after):
    return {
        "added": sorted(after.keys() - before.keys()),
        "modified": sorted(
            name for name in before.keys() & after.keys() if before[name] != after[name]
        ),
        "deleted": sorted(before.keys() - after.keys()),
    }
