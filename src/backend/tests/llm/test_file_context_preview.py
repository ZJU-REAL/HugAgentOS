"""Regression tests for file_id-only attachment requests and bounded previews."""
from core.llm.runtime import file_context as file_context_module
from core.files.content import spreadsheet_parser
from core.files import artifact_bytes as shared_artifact_bytes


def test_attachment_schema_discards_legacy_content_and_download_url():
    from core.chat.schemas import AttachmentItem

    item = AttachmentItem.model_validate(
        {
            "name": "large.txt",
            "mime_type": "text/plain",
            "file_id": "ua_123",
            "content": "must not enter the request context",
            "download_url": "/files/ua_123",
        }
    )

    assert item.model_dump() == {
        "name": "large.txt",
        "mime_type": "text/plain",
        "file_id": "ua_123",
    }


def test_file_context_injects_handles_without_reading_files(monkeypatch):
    from core.files.artifacts import reader as artifact_reader
    from core.files.content import file_parser
    from core.llm.runtime.file_context import build_file_context

    def must_not_run(*args, **kwargs):
        raise AssertionError("attachment context must not download or parse files")

    monkeypatch.setattr(artifact_reader, "fetch_parsed_text", must_not_run)
    monkeypatch.setattr(shared_artifact_bytes, "download_artifact_bytes", must_not_run)
    monkeypatch.setattr(file_parser, "parse_file", must_not_run)
    monkeypatch.setattr(spreadsheet_parser, "parse_xlsx_preview", must_not_run)

    context = build_file_context(
        [
            {"name": "report.pdf", "mime_type": "application/pdf", "file_id": "ua_pdf"},
            {
                "name": "large.xlsx",
                "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "file_id": "ua_sheet",
            },
            {"name": "photo.png", "mime_type": "image/png", "file_id": "ua_img"},
        ]
    )

    assert "- report.pdf  (file_id: ua_pdf, MIME: application/pdf)" in context
    assert "file_id: ua_sheet" in context
    assert "ua_img" not in context
    assert "read_artifact" in context
    assert "正文尚未读取" in context


def test_file_context_flags_attachment_without_file_id():
    from core.llm.runtime.file_context import build_file_context

    context = build_file_context([{"name": "orphan.txt", "mime_type": "text/plain"}])

    assert "orphan.txt" in context
    assert "缺少 file_id" in context


def test_file_context_is_empty_for_images_only():
    from core.llm.runtime.file_context import build_file_context

    assert build_file_context([{"name": "a.png", "mime_type": "image/png", "file_id": "ua_a"}]) == ""
