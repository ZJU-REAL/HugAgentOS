from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


_ARTIFACTS_PATH = Path(__file__).resolve().parents[2] / "api" / "routes" / "v1" / "artifacts.py"
_SPEC = spec_from_file_location("artifact_routes_test_module", _ARTIFACTS_PATH)
assert _SPEC and _SPEC.loader
artifact_routes = module_from_spec(_SPEC)
_SPEC.loader.exec_module(artifact_routes)


def test_extract_file_refs_supports_artifacts_array(monkeypatch):
    monkeypatch.setattr(
        artifact_routes,
        "resolve_artifact_storage_key",
        lambda file_id, storage_key=None: storage_key,
    )

    refs = artifact_routes.extract_file_refs({
        "stdout": "done",
        "artifacts": [
            {
                "file_id": "pdf_1",
                "name": "resume.pdf",
                "url": "/files/pdf_1",
                "mime_type": "application/pdf",
                "size": 1024,
            },
            {
                "file_id": "img_1",
                "name": "cover.png",
                "url": "/files/img_1",
                "mime_type": "image/png",
                "size": 256,
            },
        ],
    })

    assert refs == [
        {
            "file_id": "pdf_1",
            "name": "resume.pdf",
            "mime_type": "application/pdf",
            "size": 1024,
            "url": "/files/pdf_1",
            "storage_key": None,
        },
        {
            "file_id": "img_1",
            "name": "cover.png",
            "mime_type": "image/png",
            "size": 256,
            "url": "/files/img_1",
            "storage_key": None,
        },
    ]


def test_extract_file_refs_supports_nested_result_and_dedupes(monkeypatch):
    monkeypatch.setattr(
        artifact_routes,
        "resolve_artifact_storage_key",
        lambda file_id, storage_key=None: storage_key,
    )

    refs = artifact_routes.extract_file_refs({
        "result": {
            "artifacts": [
                {
                    "file_id": "pdf_1",
                    "name": "resume.pdf",
                    "url": "/files/pdf_1",
                    "mime_type": "application/pdf",
                    "size": 1024,
                },
                {
                    "file_id": "pdf_1",
                    "name": "resume.pdf",
                    "url": "/files/pdf_1",
                    "mime_type": "application/pdf",
                    "size": 1024,
                },
            ]
        }
    })

    assert refs == [
        {
            "file_id": "pdf_1",
            "name": "resume.pdf",
            "mime_type": "application/pdf",
            "size": 1024,
            "url": "/files/pdf_1",
            "storage_key": None,
        }
    ]
