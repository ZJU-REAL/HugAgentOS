"""Embed package web assets so opaque frames do not need authenticated subrequests.

Workspace builds must bundle JavaScript dependencies into their entry scripts.
Static HTML/CSS/images stay package-local. Neither remote code nor CSS imports load.
"""

import base64
import html
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from fastapi import HTTPException
from core.capabilities.plugins.resources.installation import safe_path
from features.sites.service import guess_site_mime

MAX_PAGE_BYTES = 8 * 1024 * 1024


def render(root, relative):
    root = Path(root)
    entry = safe_path(root, relative)
    if entry.stat().st_size > MAX_PAGE_BYTES:
        raise HTTPException(413, detail={"code": "page_too_large"})
    parser = PackagePage(root, entry.parent)
    parser.feed(entry.read_text(encoding="utf-8"))
    parser.close()
    result = "".join(parser.output)
    if len(result.encode()) > MAX_PAGE_BYTES:
        raise HTTPException(413, detail={"code": "page_too_large"})
    return result


class BoundedOutput(list):
    def __init__(self, limit=MAX_PAGE_BYTES):
        super().__init__()
        self.bytes = 0
        self.limit = limit

    def append(self, value):
        self.bytes += len(value.encode())
        if self.bytes > self.limit:
            raise HTTPException(413, detail={"code": "page_too_large"})
        super().append(value)


class PackagePage(HTMLParser):
    def __init__(self, root, directory):
        super().__init__(convert_charrefs=False)
        self.root, self.directory = root, directory
        self.output, self.skip_script = BoundedOutput(), False

    def local(self, reference, directory=None):
        url = urlsplit(reference)
        if (
            url.scheme
            or url.netloc
            or url.query
            or url.fragment
            or url.path.startswith("/")
            or "\\" in reference
        ):
            raise HTTPException(409, detail={"code": "package_asset_invalid"})
        directory = directory or self.directory
        try:
            path = safe_path(self.root, (directory / unquote(url.path)).relative_to(self.root))
        except (HTTPException, ValueError):
            raise HTTPException(409, detail={"code": "package_asset_invalid"}) from None
        if not path.is_file() or path.stat().st_size > MAX_PAGE_BYTES:
            raise HTTPException(409, detail={"code": "package_asset_invalid"})
        return path

    def data_url(self, reference, directory=None, budget=None):
        budget = MAX_PAGE_BYTES - self.output.bytes if budget is None else budget
        if reference.startswith("data:"):
            if len(reference.encode()) > budget:
                raise HTTPException(413, detail={"code": "page_too_large"})
            return reference
        path = self.local(reference, directory)
        prefix = "data:" + guess_site_mime(path.name) + ";base64,"
        if len(prefix) + 4 * ((path.stat().st_size + 2) // 3) > budget:
            raise HTTPException(413, detail={"code": "page_too_large"})
        return prefix + base64.b64encode(path.read_bytes()).decode()

    def css(self, path):
        text = path.read_text(encoding="utf-8")
        if re.search(r"@import\b", text, re.I):
            raise HTTPException(409, detail={"code": "package_css_not_bundled"})
        fragments = BoundedOutput(MAX_PAGE_BYTES - self.output.bytes - 20)
        cursor = 0
        for match in re.finditer(r"url\(\s*['\"]?([^'\"\)]+)['\"]?\s*\)", text, re.I):
            fragments.append(text[cursor : match.start()])
            asset = self.data_url(
                match[1].strip(), path.parent, fragments.limit - fragments.bytes - 10
            )
            fragments.append('url("' + asset + '")')
            cursor = match.end()
        fragments.append(text[cursor:])
        return "".join(fragments)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "base":
            return
        if tag == "script" and attrs.get("src"):
            path = self.local(attrs.pop("src"))
            code = path.read_text(encoding="utf-8")
            if re.search(
                r"\bimport\s*\(|\b(?:import|export)\s+[^;]+\sfrom\s*['\"]|\bimport\s*['\"]", code
            ):
                raise HTTPException(409, detail={"code": "package_script_not_bundled"})
            attrs.pop("integrity", None)
            attrs.pop("crossorigin", None)
            self.output.append(self.tag(tag, attrs) + code.replace("</script", "<\\/script"))
            self.skip_script = True
            return
        if tag == "link" and attrs.get("rel", "").lower() == "stylesheet":
            self.output.append(
                "<style>"
                + self.css(self.local(attrs.get("href", ""))).replace("</style", "<\\/style")
                + "</style>"
            )
            return
        if tag in {"img", "source"} and attrs.get("src"):
            attrs["src"] = self.data_url(attrs["src"])
        if attrs.get("srcset"):
            raise HTTPException(409, detail={"code": "package_asset_invalid"})
        self.output.append(self.tag(tag, attrs))

    def tag(self, tag, attrs):
        return (
            "<"
            + tag
            + "".join(
                " "
                + key
                + ('="' + html.escape(value, quote=True) + '"' if value is not None else "")
                for key, value in attrs.items()
            )
            + ">"
        )

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "script" and self.skip_script:
            self.skip_script = False
        self.output.append("</" + tag + ">")

    def handle_data(self, data):
        if not self.skip_script:
            self.output.append(data)

    def handle_entityref(self, name):
        self.output.append("&" + name + ";")

    def handle_charref(self, name):
        self.output.append("&#" + name + ";")

    def handle_comment(self, data):
        self.output.append("<!--" + data + "-->")

    def handle_decl(self, decl):
        self.output.append("<!" + decl + ">")
