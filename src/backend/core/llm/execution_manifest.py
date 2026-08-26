"""Canonical, content-addressed manifests for one LLM execution.

The manifest is deliberately an evidence record, not a second prompt store.
Prompt text, project instructions, filenames, tool descriptions and schemas are
hashed from their complete canonical representation but are never copied into
the persisted payload.  This keeps replay/diagnostic hashes useful without
widening the plaintext privacy surface.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from core.immutable import FrozenDict, freeze_json, thaw_json

MANIFEST_SCHEMA_VERSION = "harness.execution-manifest.v1"
_PUBLIC_CONTEXT_REFS = {
    "workspace_id",
    "project_id",
    "chat_mode",
    "mode_slug",
    "orchestration_profile_id",
    "workflow_policy_version",
}


def _canonical_value(value: Any) -> Any:
    """Convert supported runtime values into deterministic JSON data."""
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_canonical_value(item) for item in value]
        return sorted(normalized, key=canonical_json)
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite numbers are not canonical JSON")
        return value
    return str(value)


def canonical_json(value: Any) -> str:
    """RFC-8259-compatible stable JSON used by every manifest hash."""
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_tool_schema_order(
    schemas: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Keep the provider-facing tool prefix stable across registration order."""

    def _key(schema: Mapping[str, Any]) -> tuple[str, str]:
        function = schema.get("function")
        if not isinstance(function, Mapping):
            function = schema
        return str(function.get("name") or ""), canonical_json(schema)

    return sorted(list(schemas or []), key=_key)


def _token_estimate(text: str) -> int:
    """Cheap deterministic reserve estimate; exact provider counting is later."""
    if not text:
        return 0
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


def _context_reference_payload(context: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a non-plaintext context index suitable for persisted evidence."""
    refs: Dict[str, Any] = {}
    for key in sorted(context):
        value = context[key]
        if key in _PUBLIC_CONTEXT_REFS and isinstance(value, (str, int, bool)):
            refs[key] = value
            continue
        item: Dict[str, Any] = {
            "content_hash": stable_hash(value),
            "kind": type(value).__name__,
        }
        if isinstance(value, (str, bytes, list, tuple, set, frozenset, dict)):
            item["size"] = len(value)
        refs[key] = item
    return refs


@dataclass(frozen=True)
class PromptSectionManifest:
    id: str
    origin: str
    trust: str
    priority: int
    cache_class: str
    budget: int
    token_estimate: int
    content_hash: str
    version: str
    reference: Optional[str] = None
    sensitive: bool = False

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        if self.reference is None:
            payload.pop("reference")
        return payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PromptSectionManifest":
        return cls(
            id=str(raw.get("id") or ""),
            origin=str(raw.get("origin") or "unknown"),
            trust=str(raw.get("trust") or "runtime"),
            priority=int(raw.get("priority") or 0),
            cache_class=str(raw.get("cache_class") or "dynamic"),
            budget=int(raw.get("budget") or 0),
            token_estimate=int(raw.get("token_estimate") or 0),
            content_hash=str(raw.get("content_hash") or ""),
            version=str(raw.get("version") or "1"),
            reference=(
                str(raw["reference"]) if raw.get("reference") is not None else None
            ),
            sensitive=bool(raw.get("sensitive")),
        )


@dataclass(frozen=True)
class ToolDefinitionManifest:
    id: str
    origin: str
    trust: str
    priority: int
    cache_class: str
    budget: int
    token_estimate: int
    content_hash: str
    version: str
    description_hash: str
    schema_hash: str
    permission_hash: str
    recovery_hash: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionManifest:
    schema_version: str
    surface_generation: int
    prompt_hash: str
    prompt_manifest_hash: str
    tool_manifest_hash: str
    context_hash: str
    aggregate_hash: str
    prompt_sections: Tuple[PromptSectionManifest, ...]
    tool_definitions: Tuple[ToolDefinitionManifest, ...]
    context_refs: Mapping[str, Any]
    base_context_hash: str = ""
    context_manifest_hash: str = ""
    context_manifest: Mapping[str, Any] = field(default_factory=FrozenDict)
    _prompt_contents: Tuple[str, ...] = field(
        default_factory=tuple, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "prompt_sections", tuple(self.prompt_sections))
        object.__setattr__(self, "tool_definitions", tuple(self.tool_definitions))
        object.__setattr__(self, "context_refs", freeze_json(self.context_refs or {}))
        object.__setattr__(
            self, "base_context_hash", str(self.base_context_hash or self.context_hash)
        )
        object.__setattr__(
            self, "context_manifest", freeze_json(self.context_manifest or {})
        )
        object.__setattr__(self, "_prompt_contents", tuple(self._prompt_contents or ()))

    def prompt_section_content(self, index: int) -> str:
        """Return transient live plaintext; it is deliberately absent from ``to_dict``."""
        if 0 <= index < len(self._prompt_contents):
            return self._prompt_contents[index]
        return ""

    def with_context_manifest(self, manifest: Mapping[str, Any]) -> "ExecutionManifest":
        """Bind the sanitized, final model-request context to this execution."""
        payload = thaw_json(freeze_json(manifest or {}))
        manifest_hash = stable_hash(payload)
        base_context_hash = str(self.base_context_hash or self.context_hash)
        context_hash = stable_hash(
            {
                "base_context_hash": base_context_hash,
                "context_manifest_hash": manifest_hash,
            }
        )
        aggregate_hash = stable_hash(
            {
                "schema_version": self.schema_version,
                "surface_generation": self.surface_generation,
                "prompt_hash": self.prompt_hash,
                "prompt_manifest_hash": self.prompt_manifest_hash,
                "tool_manifest_hash": self.tool_manifest_hash,
                "context_hash": context_hash,
                "context_manifest_hash": manifest_hash,
            }
        )
        return replace(
            self,
            base_context_hash=base_context_hash,
            context_hash=context_hash,
            context_manifest_hash=manifest_hash,
            context_manifest=payload,
            aggregate_hash=aggregate_hash,
        )

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "surface_generation": self.surface_generation,
            "prompt_hash": self.prompt_hash,
            "prompt_manifest_hash": self.prompt_manifest_hash,
            "tool_manifest_hash": self.tool_manifest_hash,
            "context_hash": self.context_hash,
            "aggregate_hash": self.aggregate_hash,
            "prompt_manifest": {
                "sections": [section.to_dict() for section in self.prompt_sections]
            },
            "tool_manifest": {
                "definitions": [tool.to_dict() for tool in self.tool_definitions]
            },
            "context_refs": thaw_json(self.context_refs),
        }
        if self.context_manifest_hash:
            payload["base_context_hash"] = self.base_context_hash
            payload["context_manifest_hash"] = self.context_manifest_hash
            payload["context_manifest"] = thaw_json(self.context_manifest)
        return payload


class PromptManifestBuilder:
    """Collect ordered prompt sections and canonical tool definitions."""

    def __init__(self, *, context: Optional[Mapping[str, Any]] = None) -> None:
        self._context: Dict[str, Any] = dict(context or {})
        self._prompt_sections: list[PromptSectionManifest] = []
        self._prompt_contents: list[str] = []
        self._tool_definitions: Dict[str, ToolDefinitionManifest] = {}

    def add_prompt_section(
        self,
        section_id: str,
        content: str,
        *,
        origin: str,
        trust: str,
        priority: int,
        cache_class: str,
        budget: Optional[int] = None,
        version: str = "1",
        reference: Optional[str] = None,
        sensitive: bool = False,
    ) -> PromptSectionManifest:
        text = str(content or "")
        estimate = _token_estimate(text)
        section = PromptSectionManifest(
            id=str(section_id),
            origin=str(origin),
            trust=str(trust),
            priority=int(priority),
            cache_class=str(cache_class),
            budget=max(0, int(budget if budget is not None else estimate)),
            token_estimate=estimate,
            content_hash=stable_hash(text),
            version=str(version or "1"),
            reference=str(reference) if reference is not None else None,
            sensitive=bool(sensitive),
        )
        self._prompt_sections.append(section)
        self._prompt_contents.append(text)
        return section

    def add_prompt_section_record(
        self, record: PromptSectionManifest | Mapping[str, Any]
    ) -> None:
        """Replay a cache-safe record without needing the original plaintext."""
        if not isinstance(record, PromptSectionManifest):
            record = PromptSectionManifest.from_dict(record)
        if record.id and record.content_hash:
            self._prompt_sections.append(record)
            self._prompt_contents.append("")

    def prompt_section_records(self) -> Tuple[PromptSectionManifest, ...]:
        return tuple(self._prompt_sections)

    def prompt_section_sources(self) -> Tuple[Tuple[PromptSectionManifest, str], ...]:
        """Expose live sources to request assembly without persisting plaintext."""
        return tuple(zip(self._prompt_sections, self._prompt_contents))

    def add_tool_definition(
        self,
        schema: Mapping[str, Any],
        *,
        origin: str,
        trust: str,
        priority: int,
        cache_class: str,
        budget: Optional[int] = None,
        version: str = "1",
        permission_policy: Optional[Mapping[str, Any]] = None,
        recovery_policy: Optional[Mapping[str, Any]] = None,
    ) -> ToolDefinitionManifest:
        wrapper = dict(schema or {})
        raw_function = wrapper.get("function")
        function: Mapping[str, Any] = (
            raw_function if isinstance(raw_function, Mapping) else wrapper
        )
        name = str(function.get("name") or wrapper.get("name") or "")
        if not name:
            raise ValueError("tool definition requires a name")
        description = str(function.get("description") or "")
        parameters = function.get("parameters") or function.get("input_schema") or {}
        permission = dict(permission_policy or {})
        recovery = dict(recovery_policy or {})
        canonical_definition = {
            "name": name,
            "description": description,
            "parameters": parameters,
            "permission_policy": permission,
            "recovery_policy": recovery,
        }
        estimate = _token_estimate(canonical_json(canonical_definition))
        tool = ToolDefinitionManifest(
            id=name,
            origin=str(origin),
            trust=str(trust),
            priority=int(priority),
            cache_class=str(cache_class),
            budget=max(0, int(budget if budget is not None else estimate)),
            token_estimate=estimate,
            content_hash=stable_hash(canonical_definition),
            version=str(version or "1"),
            description_hash=stable_hash(description),
            schema_hash=stable_hash(parameters),
            permission_hash=stable_hash(permission),
            recovery_hash=stable_hash(recovery),
        )
        self._tool_definitions[name] = tool
        return tool

    def fork(self) -> "PromptManifestBuilder":
        """Clone the base prompt/context evidence for one surface generation."""
        clone = PromptManifestBuilder(context=self._context)
        clone._prompt_sections = list(self._prompt_sections)
        clone._prompt_contents = list(self._prompt_contents)
        clone._tool_definitions = dict(self._tool_definitions)
        return clone

    def build(
        self, *, final_prompt: str, surface_generation: int = 1
    ) -> ExecutionManifest:
        prompt_sections = tuple(self._prompt_sections)
        tool_definitions = tuple(
            self._tool_definitions[key] for key in sorted(self._tool_definitions)
        )
        section_payload = [section.to_dict() for section in prompt_sections]
        tool_payload = [tool.to_dict() for tool in tool_definitions]
        prompt_hash = stable_hash(str(final_prompt or ""))
        prompt_manifest_hash = stable_hash(section_payload)
        tool_manifest_hash = stable_hash(tool_payload)
        context_hash = stable_hash(self._context)
        aggregate_hash = stable_hash(
            {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "surface_generation": int(surface_generation),
                "prompt_hash": prompt_hash,
                "prompt_manifest_hash": prompt_manifest_hash,
                "tool_manifest_hash": tool_manifest_hash,
                "context_hash": context_hash,
            }
        )
        return ExecutionManifest(
            schema_version=MANIFEST_SCHEMA_VERSION,
            surface_generation=int(surface_generation),
            prompt_hash=prompt_hash,
            prompt_manifest_hash=prompt_manifest_hash,
            tool_manifest_hash=tool_manifest_hash,
            context_hash=context_hash,
            aggregate_hash=aggregate_hash,
            prompt_sections=prompt_sections,
            tool_definitions=tool_definitions,
            context_refs=_context_reference_payload(self._context),
            base_context_hash=context_hash,
            _prompt_contents=tuple(self._prompt_contents),
        )


def tool_manifest_from_schemas(
    builder: PromptManifestBuilder,
    schemas: Iterable[Mapping[str, Any]],
    *,
    builtin_tool_names: Iterable[str] = (),
) -> None:
    """Add final request schemas in name order with explicit policy hashes."""
    builtin = {str(name) for name in builtin_tool_names}
    by_name: Dict[str, Mapping[str, Any]] = {}
    for schema in stable_tool_schema_order(schemas):
        function = schema.get("function") if isinstance(schema, Mapping) else None
        name = str((function or {}).get("name") or schema.get("name") or "")
        if name:
            by_name[name] = schema
    for name in sorted(by_name):
        builder.add_tool_definition(
            by_name[name],
            origin="builtin" if name in builtin else "mcp",
            trust="platform" if name in builtin else "configured_service",
            priority=100,
            cache_class="stable",
            version="1",
            permission_policy={"decision": "allow", "source": "jx_trusted"},
            recovery_policy={"strategy": "runtime_default"},
        )


__all__ = [
    "ExecutionManifest",
    "MANIFEST_SCHEMA_VERSION",
    "PromptManifestBuilder",
    "PromptSectionManifest",
    "ToolDefinitionManifest",
    "canonical_json",
    "stable_hash",
    "stable_tool_schema_order",
    "tool_manifest_from_schemas",
]
