"""Parse PDFs through the external layout service."""
from __future__ import annotations
import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import Optional
import requests
from core.files.content import parser_config

@dataclass
class RichParseResult:
    """Layout-aware parse output: markdown plus the media the layout engine cut out.

    ``markdown`` is byte-for-byte what :func:`parse_pdf` returns, so the text path is
    unaffected. ``images`` maps the path used inside the markdown (``images/<sha>.jpg``
    in a ``![](...)`` link) to the decoded bytes, and ``blocks`` is the parsed
    ``content_list`` — one entry per layout block, images carrying ``image_caption`` /
    ``bbox`` / ``page_idx``.
    """

    markdown: str
    images: dict[str, bytes] = field(default_factory=dict)
    blocks: list[dict] = field(default_factory=list)


def post_parse(file_bytes: bytes, filename: str, mime: str, *, rich: bool) -> dict:
    """POST to the external parse service and return the single document's result dict.

    ``rich`` additionally requests the media and layout payloads. They are opt-in
    because the images come back base64-inlined in the JSON body — several MB for an
    image-heavy PDF — which the chat-attachment path has no use for.
    """
    api_url = parser_config.cfg_api_url()
    if not api_url:
        raise RuntimeError("FILE_PARSER_API_URL 未配置，无法解析 PDF 文件")

    timeout = parser_config.cfg_timeout()
    data = parser_config.cfg_parse_params()
    if rich:
        data = {**data, "return_images": "true", "return_content_list": "true"}

    try:
        resp = requests.post(
            api_url,
            files={"files": (filename, file_bytes, mime)},
            data=data,
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        raise RuntimeError(f"PDF 解析服务超时（{timeout}s），请稍后重试")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"PDF 解析服务请求失败: {e}")

    results = (resp.json() or {}).get("results", {})
    if not results:
        raise RuntimeError("PDF 解析服务返回结果为空")
    return results[next(iter(results))] or {}


def decode_data_uri(value: str) -> Optional[bytes]:
    """Decode one ``images`` entry. The service returns ``data:image/jpeg;base64,...``.

    A bare base64 payload (no data-URI prefix) is accepted too, so a service variant
    that omits the prefix still works. Undecodable entries are dropped rather than
    failing the whole document.
    """
    if not isinstance(value, str) or not value:
        return None
    payload = value.split(",", 1)[1] if value.startswith("data:") else value
    try:
        return base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        return None


def parse_content_list(raw) -> list[dict]:
    """Normalise ``content_list`` — the service returns it as a JSON **string**."""
    if isinstance(raw, list):
        return [b for b in raw if isinstance(b, dict)]
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except ValueError:
            return []
        if isinstance(data, list):
            return [b for b in data if isinstance(b, dict)]
    return []


def parse_pdf(file_bytes: bytes, filename: str = "file.pdf") -> str:
    """Parse PDF via external file-parser API. Returns markdown text."""
    content = post_parse(file_bytes, filename, "application/pdf", rich=False).get("md_content", "")
    if not content:
        raise RuntimeError("PDF 解析服务返回内容为空")
    return content


def parse_pdf_rich(file_bytes: bytes, filename: str = "file.pdf") -> RichParseResult:
    """Parse PDF keeping the figures the layout engine cut out.

    Same call as :func:`parse_pdf` plus ``return_images`` / ``return_content_list``.
    Used by knowledge-base indexing to build image assets; the chat-attachment path
    stays on the text-only call.
    """
    result = post_parse(file_bytes, filename, "application/pdf", rich=True)
    markdown = result.get("md_content", "")
    if not markdown:
        raise RuntimeError("PDF 解析服务返回内容为空")

    images: dict[str, bytes] = {}
    for name, value in (result.get("images") or {}).items():
        decoded = decode_data_uri(value)
        if decoded:
            # The markdown references the file under an ``images/`` prefix while the
            # payload is keyed by bare filename; index both so lookups by either work.
            images[str(name)] = decoded
            images.setdefault(f"images/{name}", decoded)

    return RichParseResult(
        markdown=markdown,
        images=images,
        blocks=parse_content_list(result.get("content_list")),
    )
