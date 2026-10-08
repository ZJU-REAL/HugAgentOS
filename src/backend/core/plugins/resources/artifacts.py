"""Retain downloaded resource files through the existing conversation artifact store."""
import asyncio
import mimetypes
import os
import tempfile
from pathlib import Path
import httpx
from fastapi import HTTPException
from . import store

async def retain(row, download_id):
    target = store.descriptor(row)
    limit = int(os.getenv("SANDBOX_ARTIFACT_MAX_BYTES", str(100 * 1024 * 1024)))
    with tempfile.TemporaryDirectory(prefix="resource-download-") as folder:
        path = Path(folder) / "download"
        async with httpx.AsyncClient(timeout=60, headers=target["headers"], trust_env=False) as client:
            async with client.stream("GET", target["url"] + "/download/" + download_id) as response:
                if response.status_code != 200:
                    raise HTTPException(response.status_code, "download_unavailable")
                from email.message import Message
                metadata = Message()
                metadata["Content-Disposition"] = response.headers.get("content-disposition", "")
                name = Path(metadata.get_filename() or "download").name
                total = 0
                with path.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > limit:
                            raise HTTPException(413, "download_limit")
                        output.write(chunk)
        from core.llm.tools._tool_helpers import _store_generated_file_path
        ref = await asyncio.to_thread(_store_generated_file_path, path, name=name,
            mime_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
            user_id=row.user_id, source="plugin_resource",
            extra_metadata={"chat_id": row.chat_id, "resource_id": row.resource_id})
        if not ref:
            raise HTTPException(503, "artifact_storage_unavailable")
        return {"ok": True, **ref, "artifacts": [ref]}
