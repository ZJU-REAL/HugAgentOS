"""Single-range responses for in-memory bodies (audio/video seeking)."""
import pytest

from core.infra.byte_range import byte_range_response, is_continuation_range

DATA = b"0123456789"


def _call(range_header):
    return byte_range_response(DATA, range_header, media_type="video/mp4", headers={"X-Test": "1"})


def test_without_range_sends_full_body_and_advertises_ranges():
    response = _call(None)
    assert response.status_code == 200
    assert response.body == DATA
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["x-test"] == "1"


@pytest.mark.parametrize(("header", "body", "content_range"), [
    ("bytes=2-5", b"2345", "bytes 2-5/10"),
    ("bytes=7-", b"789", "bytes 7-9/10"),
    ("bytes=-3", b"789", "bytes 7-9/10"),
    ("bytes=8-100", b"89", "bytes 8-9/10"),
    ("bytes=-100", DATA, "bytes 0-9/10"),
    ("Bytes=2-5", b"2345", "bytes 2-5/10"),
])
def test_single_range_returns_partial_content(header, body, content_range):
    response = _call(header)
    assert response.status_code == 206
    assert response.body == body
    assert response.headers["content-range"] == content_range
    assert response.headers["content-length"] == str(len(body))
    assert response.headers["content-type"] == "video/mp4"


@pytest.mark.parametrize("header", ["bytes=10-", "bytes=-0"])
def test_unsatisfiable_range_returns_416(header):
    response = _call(header)
    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */10"


@pytest.mark.parametrize("header", ["bytes=0-1,4-5", "items=0-3", "bytes=-", "bytes=5-2"])
def test_unsupported_range_falls_back_to_full_body(header):
    response = _call(header)
    assert response.status_code == 200
    assert response.body == DATA


@pytest.mark.parametrize(("header", "expected"), [
    (None, False), ("bytes=0-", False), ("bytes=-500", False), ("bytes=1024-", True), ("bytes=0-1,4-5", False),
])
def test_continuation_range_detection(header, expected):
    assert is_continuation_range(header) is expected
