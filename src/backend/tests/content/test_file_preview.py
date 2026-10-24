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
        assert command[0] == "/usr/bin/soffice"
        outdir = Path(command[command.index("--outdir") + 1])
        input_path = Path(command[-1])
        (outdir / f"{input_path.stem}.pdf").write_bytes(b"%PDF-1.4\n")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(file_routes, "find_libreoffice_binary", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(file_routes.subprocess, "run", fake_run)

    pdf_path, temp_dir = file_routes._convert_office_to_pdf(str(source), "ppt_1")

    assert Path(pdf_path).exists()
    assert Path(pdf_path).suffix == ".pdf"
    assert Path(temp_dir).is_dir()

    file_routes._cleanup_path(temp_dir)


def test_convert_office_to_pdf_explains_how_to_install_libreoffice(monkeypatch, tmp_path):
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"pptx")
    monkeypatch.setattr(file_routes, "find_libreoffice_binary", lambda: None)
    monkeypatch.setattr(
        file_routes.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("conversion must not run without LibreOffice"),
    )

    with pytest.raises(RuntimeError) as exc_info:
        file_routes._convert_office_to_pdf(str(source), "ppt_missing_lo")

    assert "重新运行一键安装器" in str(exc_info.value)
    assert "libreoffice-impress/libreoffice-writer" in str(exc_info.value)


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

    (tmp_path / 'deck.pptx').write_bytes(b'synthetic unique preview cache source')
    from core.files.content.office_preview_cache import cached_office_pdf
    monkeypatch.setattr(file_routes, "cached_office_pdf", lambda source, fid, convert:
        cached_office_pdf(source, fid, convert, root=tmp_path / "cache", version="test"))
    background_tasks = BackgroundTasks()
    response = file_routes.preview_file(
        file_id="ppt_1",
        background_tasks=background_tasks,
        format="pdf",
        user=None,
        db=object(),
    )

    assert response.media_type == "application/pdf"
    assert Path(response.path).read_bytes() == b"%PDF-1.4\n"
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
        file_routes.preview_file(
            file_id="pdf_1",
            background_tasks=BackgroundTasks(),
            format="pdf",
            user=None,
            db=object(),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Only Word (DOC/DOCX/RTF/ODT) and PowerPoint (PPT/PPTX/ODP) files support preview"


@pytest.mark.parametrize(("name", "mime_type"), [
    ("旧版合同.doc", "application/msword"),
    ("notes.rtf", "application/rtf"),
    ("draft.odt", "application/vnd.oasis.opendocument.text"),
    ("deck.odp", "application/vnd.oasis.opendocument.presentation"),
    ("unnamed", "application/msword"),
])
def test_non_ooxml_office_documents_are_previewable(name, mime_type):
    assert file_routes._is_office_previewable({"name": name, "mime_type": mime_type})


def test_spreadsheets_are_not_routed_through_office_preview():
    assert not file_routes._is_office_previewable({"name": "data.xlsx", "mime_type": ""})


def test_legacy_doc_converts_to_pdf_with_libreoffice(tmp_path):
    """Binary Word (.doc) cannot be parsed by docx-preview; the server must render it."""
    import subprocess

    from core.files.content.office import find_libreoffice_binary

    libreoffice = find_libreoffice_binary()
    if not libreoffice:
        pytest.skip("LibreOffice is not installed")
    source = tmp_path / "legacy.txt"
    source.write_text("Legacy Word document", encoding="utf-8")
    subprocess.run(
        [libreoffice, f"-env:UserInstallation={(tmp_path / 'profile').as_uri()}", "--headless",
         "--convert-to", "doc:MS Word 97", "--outdir", str(tmp_path), str(source)],
        check=True, capture_output=True, timeout=120,
    )
    doc = tmp_path / "legacy.doc"
    assert doc.read_bytes()[:8] == bytes.fromhex("d0cf11e0a1b11ae1")  # OLE2 container, not OOXML

    pdf_path, temp_dir = file_routes._convert_office_to_pdf(str(doc), "doc_1")
    try:
        assert Path(pdf_path).read_bytes().startswith(b"%PDF")
    finally:
        file_routes._cleanup_path(temp_dir)


def test_direct_download_supports_range_for_media_seeking(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"0123456789")
    item = {"name": "clip.mp4", "mime_type": "video/mp4", "path": str(media)}
    app = FastAPI()

    @app.get("/clip")
    def clip(background_tasks: BackgroundTasks):
        return file_routes._build_direct_download_response(
            item=item, file_id="clip", inline=True, background_tasks=background_tasks,
        )

    with TestClient(app) as client:
        full = client.get("/clip")
        partial = client.get("/clip", headers={"Range": "bytes=6-"})

    assert full.headers["accept-ranges"] == "bytes"
    assert partial.status_code == 206
    assert partial.content == b"6789"
    assert partial.headers["content-range"] == "bytes 6-9/10"
    assert partial.headers["content-disposition"].startswith("inline")
