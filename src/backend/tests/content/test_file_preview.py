import asyncio
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException


_FILES_PATH = Path(__file__).resolve().parents[2] / "api" / "routes" / "files.py"
_SPEC = spec_from_file_location("file_routes_test_module", _FILES_PATH)
assert _SPEC and _SPEC.loader
file_routes = module_from_spec(_SPEC)
_SPEC.loader.exec_module(file_routes)


def test_convert_office_to_pdf_returns_generated_pdf(monkeypatch, tmp_path):
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"pptx")

    def fake_run(command, capture_output, text, timeout):
        outdir = Path(command[command.index("--outdir") + 1])
        input_path = Path(command[-1])
        (outdir / f"{input_path.stem}.pdf").write_bytes(b"%PDF-1.4\n")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(file_routes.subprocess, "run", fake_run)

    pdf_path, temp_dir = file_routes._convert_office_to_pdf(str(source), "ppt_1")

    assert Path(pdf_path).exists()
    assert Path(pdf_path).suffix == ".pdf"
    assert Path(temp_dir).is_dir()

    file_routes._cleanup_path(temp_dir)


def test_preview_file_returns_inline_pdf_for_powerpoint(monkeypatch, tmp_path):
    pdf_path = tmp_path / "preview.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    temp_dir = tmp_path / "preview-work"
    temp_dir.mkdir()

    monkeypatch.setattr(file_routes, "_load_artifact_item", lambda file_id, db: {
        "name": "季度汇报.pptx",
        "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "size": 1024,
        "metadata": {},
    })
    monkeypatch.setattr(file_routes, "_prepare_local_file", lambda **kwargs: str(tmp_path / "deck.pptx"))
    monkeypatch.setattr(file_routes, "_convert_office_to_pdf", lambda source_path, file_id: (str(pdf_path), str(temp_dir)))

    background_tasks = BackgroundTasks()
    response = asyncio.run(file_routes.preview_file(
        file_id="ppt_1",
        background_tasks=background_tasks,
        format="pdf",
        user=None,
        db=object(),
    ))

    assert response.media_type == "application/pdf"
    assert response.path == str(pdf_path)
    assert response.filename == "季度汇报.pdf"
    assert len(background_tasks.tasks) == 1


def test_preview_file_rejects_non_powerpoint(monkeypatch):
    monkeypatch.setattr(file_routes, "_load_artifact_item", lambda file_id, db: {
        "name": "report.pdf",
        "mime_type": "application/pdf",
        "size": 1024,
        "metadata": {},
    })

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(file_routes.preview_file(
            file_id="pdf_1",
            background_tasks=BackgroundTasks(),
            format="pdf",
            user=None,
            db=object(),
        ))

    assert exc_info.value.status_code == 400
    # Production code files.py has extended the preview hint message to also support Word (feature drift, unrelated to the agentscope migration)
    assert exc_info.value.detail == "Only Word (DOC/DOCX) and PowerPoint (PPT/PPTX) files support preview"
