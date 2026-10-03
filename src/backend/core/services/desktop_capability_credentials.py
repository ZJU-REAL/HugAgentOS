"""Collect private credentials without mistaking public model settings for secrets."""

from __future__ import annotations

import re
from typing import Any, Dict
from urllib.parse import unquote, unquote_plus, urlsplit

from core.llm.reasoning_effort import EFFORT_KEYS


class CapabilityContentRejected(ValueError):
    """A fixed diagnostic; never include the matching credential or content."""

    def __init__(self):
        super().__init__(
            "capability content contains configured credentials or credential policy is unavailable"
        )


# Match credential-bearing field names by whole segment, not substring: a field
# named ``MAX_OUTPUT_TOKENS`` (a numeric limit) must not be treated as a token
# credential just because "TOKENS" contains "token" — otherwise its numeric value
# is scanned as a secret and coincidentally matches bytes in unrelated skill/agent
# bundles, blocking every download with a false "integrity_failed".
_SECRET_FIELD = re.compile(
    r"(?<![A-Za-z0-9])(?:secret|token|password|credential|api[_-]?key|access[_-]?key"
    r"|private[_-]?key|authorization|cookie|key)(?![A-Za-z0-9])",
    re.I,
)
_URL_FIELDS = frozenset({"url", "base_url", "baseurl", "endpoint", "server_url", "api_url", "uri"})


def _secrets_from_config(config: Dict[str, Any], *, model_config: bool = False) -> set[str]:
    found: set[str] = set()

    def add(value):
        if isinstance(value, str) and value.strip():
            found.add(value)
            if value.lower().startswith(("bearer ", "basic ")):
                found.add(value.split(" ", 1)[1].strip())
        elif isinstance(value, dict):
            for nested in value.values():
                add(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                add(nested)

    def url_credentials(value):
        if not isinstance(value, str):
            return
        try:
            parsed = urlsplit(value)
            if parsed.password is not None:
                add(parsed.password)
                add(unquote(parsed.password))
                userinfo = parsed.netloc.rsplit("@", 1)[0]
                add(userinfo)
                add(unquote(userinfo))
            elif parsed.username is not None:
                add(parsed.username)
                add(unquote(parsed.username))
            for pair in parsed.query.split("&"):
                raw_key, separator, raw_value = pair.partition("=")
                key = unquote_plus(raw_key)
                if separator and (_SECRET_FIELD.search(key) or key.lower() in ("sig", "signature")):
                    add(raw_value)
                    add(unquote_plus(raw_value))
        except (ValueError, UnicodeError):
            raise CapabilityContentRejected() from None

    def walk(value, path=()):
        # Only this typed model field uses "key" as a public enum identifier.
        # Keep scanning unknown keys, sibling credentials and connector configs.
        if isinstance(value, dict):
            for key, item in value.items():
                is_effort_label = (
                    model_config
                    and len(path) == 3
                    and path[:2] == ("extra_config", "reasoning_effort_levels")
                    and isinstance(path[2], int)
                    and key == "key"
                    and isinstance(item, str)
                    and item in EFFORT_KEYS
                )
                if is_effort_label:
                    continue
                if _SECRET_FIELD.search(str(key)):
                    add(item)
                elif str(key).lower() in _URL_FIELDS:
                    url_credentials(item)
                elif isinstance(item, (dict, list, tuple)):
                    walk(item, path + (str(key),))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(item, path + (index,))

    walk(config)
    return {value for value in found if value}
