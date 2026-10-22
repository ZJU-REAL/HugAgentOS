"""Attachment context policy using immutable hook inputs and neutral content."""
import asyncio
from core.files import artifact_bytes
from core.harness.hooks import Decision, FailureMode, HookSpec, HookStage, Invocation
from core.llm.runtime import file_context
from core.models.vision import resolve_vision_mode


def supports_native_images(provider_id: str) -> bool:
    return resolve_vision_mode(provider_id or "") == "native"


def native_image_content(image_files: list, user_id):
    blocks, names = [], []
    for file in image_files:
        result = file_context.fetch_image_base64(file, user_id=user_id)
        if result:
            encoded, mime = result
            blocks.append({"type": "data", "source": {
                "type": "base64", "media_type": mime, "data": encoded,
            }})
            names.append(file.get("name", "图片"))
    if not blocks:
        return None
    prefix = {"type": "text", "text": f"[用户上传了 {len(blocks)} 张图片：{'、'.join(names)}]"}
    return [prefix, *blocks]


async def vision_evidence(image_files: list, user_id, precomputed: str = "") -> str:
    if precomputed.strip():
        return precomputed.strip()
    from core.models.vision.attachments import transcribe_attachments
    result = await transcribe_attachments(image_files, user_id=user_id,
                                          download_bytes=artifact_bytes.download_artifact_bytes)
    return result.text if result is not None and result.text else ""


def payload(content, *, kind, origin, trust, token_budget=8000):
    return dict(content=content, kind=kind, origin=origin, trust=trust,
                priority=650, token_budget=token_budget)


class AttachmentContextHook:
    def __init__(self):
        self.injected = False

    async def __call__(self, invocation: Invocation) -> Decision:
        if invocation.operation_name != "agent_reply":
            return Decision()
        runtime = invocation.data.get("runtime", {})
        uploaded = list(runtime.get("uploaded_files") or [])
        historical = list(runtime.get("historical_files") or [])
        if self.injected or not (uploaded or historical):
            return Decision()
        self.injected = True
        user_id = runtime.get("user_id") or None
        items = list(invocation.data.get("context_items") or [])
        if historical:
            text = file_context.build_historical_files_context(historical)
            if text:
                items.append(payload(text, kind="project_material", origin="workspace:historical_files", trust="workspace"))
        text = await asyncio.to_thread(file_context.build_file_context, uploaded, user_id=user_id) if uploaded else ""
        if text:
            items.append(payload(text, kind="attachment", origin="user:uploaded_files", trust="user"))
        images = [file for file in uploaded if artifact_bytes.is_image(file)]
        if images:
            if supports_native_images(str(runtime.get("model_provider_id") or "")):
                content = await asyncio.to_thread(native_image_content, images, user_id)
                if content:
                    items.append(payload(content, kind="attachment", origin="user:uploaded_images", trust="user", token_budget=16000))
            else:
                text = await vision_evidence(images, user_id, str(runtime.get("vision_evidence_text") or ""))
                if text:
                    items.append(payload(text, kind="attachment", origin="vision:transcription", trust="tool"))
        return Decision.modify({"context_items": items})


def attachment_context_hook() -> HookSpec:
    return HookSpec("attachment_context", HookStage.TRANSFORM_CONTEXT, AttachmentContextHook(),
                    priority=100, timeout_ms=180000, failure_mode=FailureMode.OPEN,
                    mutable_fields=frozenset({"context_items"}))
