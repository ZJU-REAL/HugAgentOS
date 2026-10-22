"""Implementation for MCP tool: web_fetch.

Fetches a URL and extracts content as text, markdown, or raw HTML.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup, Tag

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# Tags to remove when extracting content
_REMOVE_TAGS = {"script", "style", "nav", "footer", "header", "noscript", "iframe", "svg"}


MAX_RESPONSE_BYTES = 2_000_000
MAX_CONTENT_CHARS = 100_000


async def fetch_url(
    url: str,
    extract_mode: Literal["text", "markdown", "html"] = "text",
    max_chars: int = 50000,
) -> dict:
    """Fetch bounded page content with source and truncation metadata."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("An absolute HTTP(S) URL is required")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URLs must not contain credentials")
    if extract_mode not in {"text", "markdown", "html"}:
        raise ValueError("Invalid extract mode")
    if type(max_chars) is not int or not 1 <= max_chars <= MAX_CONTENT_CHARS:
        raise ValueError("max_chars must be between 1 and 100000")
    async with asyncio.timeout(25):
        async with httpx.AsyncClient(
            headers=_HEADERS,
            timeout=20.0,
            follow_redirects=True,
            max_redirects=5,
        ) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                chunks = []
                size = 0
                download_truncated = False
                async for chunk in resp.aiter_bytes():
                    room = MAX_RESPONSE_BYTES - size
                    chunks.append(chunk[:room])
                    size += min(len(chunk), room)
                    if len(chunk) > room:
                        download_truncated = True
                        break
                html = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
                final_url = str(resp.url)

    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else None
    if extract_mode == "html":
        content = html
    else:
        for tag in soup.find_all(list(_REMOVE_TAGS)):
            tag.decompose()
        content = _to_markdown(soup) if extract_mode == "markdown" else _to_text(soup)
    text = content[:max_chars]
    return {
        "result": text,
        "url": url,
        "final_url": final_url,
        "title": title,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "published_date": None,
        "returned_chars": len(text),
        "content_truncated": download_truncated or len(content) > max_chars,
        "download_truncated": download_truncated,
    }


def _to_text(soup: BeautifulSoup) -> str:
    """Extract clean text from parsed HTML."""
    text: str = soup.get_text(separator="\n", strip=True)
    # Remove any residual <style>...</style> or <script>...</script> that survived parsing
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Remove orphaned opening tags (e.g. <style ...> without closing tag)
    text = re.sub(r"<(?:style|script)[^>]*>[^<]*", "", text, flags=re.IGNORECASE)
    # Collapse multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _to_markdown(soup: BeautifulSoup) -> str:
    """Convert parsed HTML to simplified Markdown."""
    parts: list[str] = []

    body = soup.find("body") or soup

    for elem in body.descendants:
        if not isinstance(elem, Tag):
            continue

        tag = elem.name

        # Headings
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = int(tag[1])
            text = elem.get_text(strip=True)
            if text:
                parts.append(f"\n{'#' * level} {text}\n")

        # Paragraphs
        elif tag == "p":
            text = elem.get_text(strip=True)
            if text:
                parts.append(f"\n{text}\n")

        # Links (only process if direct child text)
        elif tag == "a":
            text = elem.get_text(strip=True)
            href = elem.get("href", "")
            if text and href and not href.startswith("#") and not href.startswith("javascript:"):
                parts.append(f"[{text}]({href})")

        # List items
        elif tag == "li":
            text = elem.get_text(strip=True)
            if text:
                parts.append(f"- {text}")

        # Blockquotes
        elif tag == "blockquote":
            text = elem.get_text(strip=True)
            if text:
                quoted = "\n".join(f"> {line}" for line in text.split("\n"))
                parts.append(f"\n{quoted}\n")

        # Pre/code blocks
        elif tag == "pre":
            text = elem.get_text(strip=False)
            if text.strip():
                parts.append(f"\n```\n{text.strip()}\n```\n")

        # Tables
        elif tag == "table":
            table_md = _table_to_markdown(elem)
            if table_md:
                parts.append(f"\n{table_md}\n")

    result = "\n".join(parts)
    # Collapse excessive newlines
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def _table_to_markdown(table: Tag) -> str:
    """Convert an HTML table to Markdown table."""
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = [(td.get_text(strip=True) or "") for td in tr.find_all(["td", "th"])]
        if cells:
            rows.append(cells)

    if not rows:
        return ""

    # Normalize column count
    max_cols = max(len(r) for r in rows)
    for r in rows:
        while len(r) < max_cols:
            r.append("")

    lines = []
    # Header row
    lines.append("| " + " | ".join(rows[0]) + " |")
    lines.append("| " + " | ".join("---" for _ in rows[0]) + " |")
    # Data rows
    for row in rows[1:]:
        lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines)
