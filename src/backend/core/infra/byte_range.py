"""HTTP single-range responses for content already held in memory.

Starlette's ``FileResponse`` answers ``Range`` requests by itself; routes that
return ``Response(bytes)`` do not, so browsers cannot seek inside audio/video
served by them. Multi-range requests are answered with the full body (200),
which RFC 9110 allows.
"""

import re
from typing import Mapping, Optional

from starlette.responses import Response

_SINGLE_RANGE = re.compile(r"bytes=(\d*)-(\d*)", re.IGNORECASE)


def _resolve(range_header: str, size: int) -> Optional[tuple[int, int]]:
    """Return the inclusive ``(start, end)`` span, ``None`` to send the full body,
    or raise ``ValueError`` when the range cannot be satisfied."""
    match = _SINGLE_RANGE.fullmatch(range_header.strip())
    if not match or match.groups() == ("", ""):
        return None
    first, last = match.groups()
    if first and last and int(first) > int(last):
        # An invalid range-spec is ignored (RFC 9110 §14.2), not refused.
        return None
    if first:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    else:
        suffix = int(last)
        if suffix == 0:
            raise ValueError("empty suffix range")
        start, end = max(size - suffix, 0), size - 1
    if start >= size:
        raise ValueError("range outside content")
    return start, end


def is_continuation_range(range_header: Optional[str]) -> bool:
    """True for a range that resumes mid-file — the follow-up requests a media
    player sends while seeking, which should not be audited as new reads."""
    match = _SINGLE_RANGE.fullmatch((range_header or "").strip())
    return bool(match and match.group(1) and int(match.group(1)) > 0)


def byte_range_response(
    data: bytes,
    range_header: Optional[str],
    *,
    media_type: str,
    headers: Mapping[str, str],
) -> Response:
    """Serve ``data`` whole (200), as one slice (206) or as unsatisfiable (416)."""
    base = {**headers, "Accept-Ranges": "bytes"}
    if not range_header:
        return Response(data, media_type=media_type, headers=base)
    size = len(data)
    try:
        span = _resolve(range_header, size)
    except ValueError:
        return Response(status_code=416, headers={**base, "Content-Range": f"bytes */{size}"})
    if span is None:
        return Response(data, media_type=media_type, headers=base)
    start, end = span
    return Response(
        data[start : end + 1],
        status_code=206,
        media_type=media_type,
        headers={**base, "Content-Range": f"bytes {start}-{end}/{size}"},
    )
