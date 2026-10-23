"""Context items for the references listed in an execution manifest."""
from __future__ import annotations
from typing import Any, Mapping
from core.llm.context.ir_constants import KIND_PROJECT, KIND_SYSTEM_RULE, POLICY_HEAD_TAIL, POLICY_NEVER, VISIBILITY_MANIFEST_ONLY, VISIBILITY_MODEL
from core.llm.context.items import ContextItem

def reference_items_from_execution_manifest(self, manifest: Any) -> list[ContextItem]:
    if hasattr(manifest, "to_dict"):
        payload = manifest.to_dict()
    elif isinstance(manifest, Mapping):
        payload = dict(manifest)
    else:
        return []
    sections = (payload.get("prompt_manifest") or {}).get("sections") or []
    items: list[ContextItem] = []
    for index, section in enumerate(sections):
        if not isinstance(section, Mapping):
            continue
        section_id = str(section.get("id") or "")
        content_hash = str(section.get("content_hash") or "")
        if not section_id or not content_hash:
            continue
        is_project = section_id == "runtime/project"
        live_content = ""
        content_reader = getattr(manifest, "prompt_section_content", None)
        if callable(content_reader):
            live_content = str(content_reader(index) or "")
        if is_project and live_content:
            items.append(
                ContextItem.create(
                    item_id=f"prompt:{index}:{section_id}",
                    kind=KIND_PROJECT,
                    origin=str(section.get("origin") or "workspace:project"),
                    trust=str(section.get("trust") or "workspace"),
                    visibility=VISIBILITY_MODEL,
                    priority=int(section.get("priority") or 900),
                    token_budget=max(
                        1,
                        int(section.get("budget") or section.get("token_estimate") or 1),
                    ),
                    truncation_policy=POLICY_HEAD_TAIL,
                    content=live_content,
                    content_ref=(
                        str(section["reference"]) if section.get("reference") else None
                    ),
                    content_hash=content_hash,
                    token_estimate=int(section.get("token_estimate") or 0),
                    cache_class=str(section.get("cache_class") or "workspace"),
                    created_seq=1,
                    render_role="system",
                    render_name="system",
                    message_group=f"prompt:{index}",
                )
            )
            continue
        items.append(
            ContextItem.create(
                item_id=f"prompt:{index}:{section_id}",
                kind=KIND_PROJECT if is_project else KIND_SYSTEM_RULE,
                origin=str(section.get("origin") or "prompt:unknown"),
                trust=str(section.get("trust") or "platform"),
                visibility=VISIBILITY_MANIFEST_ONLY,
                priority=int(section.get("priority") or 0),
                token_budget=int(section.get("budget") or 0),
                truncation_policy=POLICY_NEVER,
                content=None,
                content_ref=str(
                    section.get("reference")
                    or f"prompt:{section_id}@{section.get('version') or '1'}"
                ),
                content_hash=content_hash,
                token_estimate=int(section.get("token_estimate") or 0),
                cache_class=str(section.get("cache_class") or "stable"),
                created_seq=-10_000 + index,
                render_role="system",
                message_group=f"prompt:{index}",
            )
        )
    return items
