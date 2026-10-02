from core.content import artifact_refs as artifact_routes


def test_extract_file_refs_supports_artifacts_array():
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


def test_extract_file_refs_supports_nested_result_and_dedupes():
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
