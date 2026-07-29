"""Regression tests for file_id-only attachment requests and bounded previews."""


def test_attachment_schema_discards_legacy_content_and_download_url():
    from api.schemas import AttachmentItem

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


def test_regular_file_context_uses_bounded_lazy_preview(monkeypatch):
    from core.content import artifact_reader
    from core.llm.hooks import _build_file_context

    parsed = "A" * artifact_reader.ATTACHMENT_PREVIEW_MAX_CHARS + "UNSEEN_TAIL"
    monkeypatch.setattr(artifact_reader, "fetch_parsed_text", lambda *args, **kwargs: parsed)

    context = _build_file_context(
        [
            {
                "name": "large.txt",
                "mime_type": "text/plain",
                "file_id": "ua_123",
            }
        ],
        user_id="user_1",
    )

    assert "file_id: ua_123" in context
    assert f"本轮自动展示 {artifact_reader.ATTACHMENT_PREVIEW_MAX_CHARS} 字符" in context
    assert "read_artifact" in context
    assert "UNSEEN_TAIL" not in context
    assert "batch_plan" not in context


def test_xlsx_context_uses_preview_budget_without_batch_hint(monkeypatch):
    from core.content import artifact_reader
    from core.content import file_parser
    from core.llm import hooks

    seen = {}

    monkeypatch.setattr(hooks, "_download_artifact_bytes", lambda *args, **kwargs: b"xlsx")
    monkeypatch.setattr(artifact_reader, "fetch_parsed_text", lambda *args, **kwargs: "cached")

    def fake_preview(_file_bytes, char_budget):
        seen["char_budget"] = char_budget
        return {
            "total_rows": 1000,
            "total_columns": 3,
            "preview_rows": 12,
            "header": ["姓名", "部门", "备注"],
            "preview_md": "| 姓名 | 部门 | 备注 |\n| --- | --- | --- |",
            "truncated_columns": False,
            "truncated_cells": 0,
        }

    monkeypatch.setattr(file_parser, "parse_xlsx_preview", fake_preview)

    block = hooks._build_xlsx_preview_block(
        {
            "name": "large.xlsx",
            "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "file_id": "ua_sheet",
        },
        user_id="user_1",
    )

    assert seen["char_budget"] == artifact_reader.ATTACHMENT_PREVIEW_MAX_CHARS
    assert block is not None
    assert "总规模: 1000 行 × 3 列" in block
    assert "列名: [姓名, 部门, 备注]" in block
    assert "read_artifact" in block
    assert "batch_plan" not in block
