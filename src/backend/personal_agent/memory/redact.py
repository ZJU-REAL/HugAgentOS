"""Credentials never enter memory: Dreaming redacts sources, lint refuses pages that hold any.

The schema asks for a secret's name, never its value ("数据库密码：存在 1Password 的 prod-db
条目里"), so a key word alone is not a secret: the value next to it has to look like one.
"""
import re

_PRIVATE_KEY = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S)
# Known token shapes. sk- only at a word start: "task-<id>" (a source id) is not a key.
_TOKEN = re.compile(
    r"(?:Bearer\s+[A-Za-z0-9_.~+/-]{16,}"
    r"|(?<![A-Za-z0-9])(?:sk|rk|pk)[-_](?:live_|test_|proj-)?[A-Za-z0-9_-]{16,}"
    r"|(?<![A-Za-z0-9])(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AIza[0-9A-Za-z_-]{35})")
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+:)([^/\s@]+)(@)")
_KEY = r"(?<![A-Za-z0-9_])(?:password|passwd|pwd|api[_ -]?key|access[_ -]?token|auth[_ -]?token|token|secret|密码|密钥|口令)"
# A value that looks like a secret: ASCII, at least 6 characters, no CJK (a description).
_VALUE = r"[A-Za-z0-9!@#$%^&*_.+/=~-]{6,}"
_QUOTED = re.compile(r'''(?im)((?:["']?)''' + _KEY + r'''["']?\s*[:=：]\s*)(["'])([^"'\n]{6,})\2''')
_BARE = re.compile(r"(?im)(" + _KEY + r"\s*[:=：]\s*)(" + _VALUE + r")(?![^\s,，;；])")
_SPOKEN = re.compile(r"(?i)((?:password|passwd|密码|口令)\s*(?:is|是|为)\s*)(" + _VALUE + r")")


def redact(value):
    value = _PRIVATE_KEY.sub("[REDACTED]", value)
    value = _TOKEN.sub("[REDACTED]", value)
    value = _JWT.sub("[REDACTED]", value)
    value = _URL_CREDENTIALS.sub(r"\1[REDACTED]\3", value)
    value = _QUOTED.sub(lambda m: m[1] + m[2] + "[REDACTED]" + m[2], value)
    value = _SPOKEN.sub(r"\1[REDACTED]", value)
    return _BARE.sub(r"\1[REDACTED]", value)


def holds_secret(value):
    return redact(value) != value
