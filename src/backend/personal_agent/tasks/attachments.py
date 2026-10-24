"""Stage explicitly attached task files for initial and steered inputs."""
import asyncio
import posixpath
from core.db.models import Artifact
from core.db.artifact_scope import is_personal_artifact
from core.db.threads import run_in_session


async def stage_inputs(task_id, uid, box, artifact_ids):
    if not artifact_ids:
        return ""
    from core.files.storage import get_storage
    from personal_agent.computer.sandbox import get_personal_sandbox_provider
    provider = get_personal_sandbox_provider()
    prompt = ""
    for artifact_id in artifact_ids:
        def _attachment(db):
            artifact = db.get(Artifact, artifact_id)
            if not artifact or artifact.user_id != uid or not is_personal_artifact(artifact) or artifact.deleted_at:
                raise PermissionError("Task attachment no longer available")
            filename, key, size = artifact.filename, artifact.storage_key, artifact.size_bytes
            return size, key, filename
        size, key, filename = await run_in_session(_attachment)
        if size > 20 * 1024 * 1024:
            raise ValueError("Attachment exceeds 20 MiB")
        path = "/workspace/work/" + task_id + "/inputs/" + artifact_id + "/" + posixpath.basename(filename)
        content = await asyncio.to_thread(get_storage().download_bytes, key)
        await provider.put_file(box, path, content, user_id=uid)
        prompt += "\nExplicit task attachment: " + path
    return prompt
