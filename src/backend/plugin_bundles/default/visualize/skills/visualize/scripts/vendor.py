"""Modified for HugAgentOS: reject resources absent from the offline export."""
"""Use the pinned browser distributions in standalone HTML without a network request."""

import base64
import hashlib
import json
import re
from html import escape
from html.parser import HTMLParser
from pathlib import Path

_VENDOR = Path(__file__).resolve().parents[1] / "assets" / "vendor"


def embed_vendor_scripts(html: str) -> str:
    manifest = json.loads((_VENDOR / "manifest.json").read_text(encoding="utf-8"))
    resources = {entry["url"]: entry for entry in manifest["resources"]}
    if len(resources) != len(manifest["resources"]):
        raise ValueError("duplicate bundled resource URL")
    data_urls: dict[str, str] = {}
    offsets = [0] + [match.end() for match in re.finditer("\n", html)]
    replacements: list[tuple[int, int, str]] = []

    class Parser(HTMLParser):
        def handle_starttag(self, tag, attrs):
            resource_attributes = {
                "script": {"src"}, "link": {"href"}, "img": {"src", "srcset"},
                "source": {"src", "srcset"}, "video": {"src", "poster"},
                "audio": {"src"}, "iframe": {"src"}, "embed": {"src"},
                "object": {"data"}, "image": {"href", "xlink:href"},
                "use": {"href", "xlink:href"},
            }
            for name, value in attrs:
                if name not in resource_attributes.get(tag, set()) or not value:
                    continue
                if tag == "script" and value in resources:
                    continue
                if not value.startswith(("data:", "#")):
                    raise ValueError(f"unbundled resource must be embedded before export: {value}")
            if tag != "script":
                return
            src = next((value for name, value in attrs if name == "src"), None)
            entry = resources.get(src)
            if entry is None:
                return
            if src not in data_urls:
                file = entry["file"]
                if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.js", file):
                    raise ValueError("invalid bundled resource path")
                contents = (_VENDOR / file).read_bytes()
                if hashlib.sha256(contents).hexdigest() != entry["sha256"]:
                    raise ValueError(f"bundled resource hash mismatch: {file}")
                data_urls[src] = "data:text/javascript;base64," + base64.b64encode(contents).decode("ascii")
            # 保留 async/id/onload 等属性；只换 src，不改变脚本顺序或重写作者的脚本正文。
            attributes = " ".join(
                name if value is None else f'{name}="{escape(data_urls[src] if name == "src" else value, quote=True)}"'
                for name, value in attrs
            )
            line, column = self.getpos()
            start = offsets[line - 1] + column
            raw = self.get_starttag_text()
            replacements.append((start, start + len(raw), "<script " + attributes + ("/>" if raw.endswith("/>") else ">")))

    if re.search(r"@import\s", html, re.IGNORECASE):
        raise ValueError("CSS imports must be embedded before export")
    for match in re.finditer(r"url\(\s*([^)]+)\)", html, re.IGNORECASE):
        resource = match.group(1).strip().strip("\"'")
        if not resource.startswith(("data:", "#")):
            raise ValueError("CSS resources must be embedded before export")
    Parser(convert_charrefs=False).feed(html)
    parts = []
    previous = 0
    for start, end, replacement in replacements:
        parts.extend((html[previous:start], replacement))
        previous = end
    parts.append(html[previous:])
    return "".join(parts)
