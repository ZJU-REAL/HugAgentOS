"""Selftest: report_export_mcp fallback on docx conversion failure."""

from __future__ import annotations


def main() -> int:
    from mcp_servers.report_export_mcp import impl

    original = impl._markdown_to_docx_bytes

    def _raise_failure(markdown: str, title: str) -> bytes:
        _ = markdown, title
        raise RuntimeError("python-docx is not installed")

    try:
        impl._markdown_to_docx_bytes = _raise_failure
        out = impl.export_report_to_docx(markdown="# demo", title="demo")
    finally:
        impl._markdown_to_docx_bytes = original

    assert isinstance(out, dict)
    assert out.get("ok") is False
    assert "python-docx" in str(out.get("error", ""))
    print("report_export_fallback_selftest: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
