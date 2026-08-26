"""Runtime Binder — freeze the asset versions one run uses (GCE ticket 03).

Today an admin can publish a new skill or activate a new prompt while a long
answer is still streaming, and nobody can say afterwards whether that run used
the old or the new one.  The binder removes the ambiguity: at run start we
resolve every governed asset to a concrete version, hash the set into a bundle
id, and pin it for the rest of the run.

Two properties matter more than completeness:

* **No drift.**  Once bound, the run keeps its versions even if the underlying
  asset is republished mid-flight.
* **Never fail the turn.**  Binding is bookkeeping.  If a resolver raises, the
  bundle degrades to partial and the user still gets their answer — an
  observability feature must not become an availability risk.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.evolution.contract import (
    ASSET_KB,
    ASSET_MEMORY,
    ASSET_MODEL,
    ASSET_ONTOLOGY,
    ASSET_PROMPT,
    ASSET_SKILL,
    ASSET_WORKFLOW,
    AssetBundle,
    AssetRef,
    build_bundle,
)

logger = logging.getLogger(__name__)

# Bundles are small and short-lived; a bounded in-process cache keeps the hot
# path free of database reads. The Episode assembler persists what it needs.
_MAX_CACHED_BUNDLES = 512
_bundles: Dict[str, AssetBundle] = {}
_run_to_bundle: Dict[str, str] = {}
_lock = threading.Lock()


def _remember(bundle: AssetBundle, run_id: Optional[str]) -> None:
    with _lock:
        _bundles[bundle.bundle_id] = bundle
        if run_id:
            _run_to_bundle[run_id] = bundle.bundle_id
        if len(_bundles) > _MAX_CACHED_BUNDLES:
            # Cheap eviction: drop the oldest insertion. Losing a cached bundle
            # is harmless — it is re-resolvable from the persisted Episode.
            for stale_key in list(_bundles.keys())[: len(_bundles) - _MAX_CACHED_BUNDLES]:
                _bundles.pop(stale_key, None)


def get_bundle(bundle_id: str) -> Optional[AssetBundle]:
    with _lock:
        return _bundles.get(bundle_id)


def resolve_bundle_for_run(run_id: str) -> Optional[AssetBundle]:
    with _lock:
        bundle_id = _run_to_bundle.get(run_id)
        return _bundles.get(bundle_id) if bundle_id else None


def clear_run_binding(run_id: Optional[str]) -> None:
    """Fail closed when final request evidence could not be rebound.

    The content-addressed bundle may still be referenced by another run, so we
    only clear this run's index.  A resolver must never fall back to the stale
    pre-context bundle and describe it as complete request evidence.
    """
    if not run_id:
        return
    with _lock:
        _run_to_bundle.pop(str(run_id), None)


def _safe(resolver, label: str, degraded: List[str]) -> List[AssetRef]:
    """Run one resolver, converting any failure into a partial-bundle marker."""
    try:
        return list(resolver() or [])
    except Exception as exc:
        logger.debug("[binder] %s resolution failed: %s", label, exc)
        degraded.append(label)
        return []


def _prompt_refs() -> List[AssetRef]:
    from core.services import prompt_version_service

    refs: List[AssetRef] = []
    for kind in ("system", "code_exec", "distillation", "plan_mode", "turbo"):
        try:
            active = prompt_version_service.get_active_version(kind)
        except Exception:
            continue
        if active:
            refs.append(
                AssetRef(
                    kind=ASSET_PROMPT,
                    asset_id=kind,
                    version=str(active.get("id") or "default"),
                    detail={"label": active.get("label") or ""},
                )
            )
    return refs


def _prompt_refs_from_manifest(manifest: Dict[str, Any]) -> List[AssetRef]:
    """Use the exact prompt sections already rendered for this request.

    Reading the active prompt pointer again after rendering has a publish race:
    the manifest may describe A while a concurrent activation makes the binder
    observe B. Section records carry the resolved version/content hash and are
    therefore the authoritative request snapshot.
    """
    prompt_manifest = manifest.get("prompt_manifest") or {}
    sections = prompt_manifest.get("sections") or []
    refs: List[AssetRef] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("id") or "")
        content_hash = str(section.get("content_hash") or "")
        if not section_id or not content_hash:
            continue
        refs.append(
            AssetRef(
                kind=ASSET_PROMPT,
                asset_id=section_id,
                version=str(section.get("version") or content_hash),
                detail={
                    "origin": str(section.get("origin") or "unknown"),
                    "content_hash": content_hash,
                    "trust": str(section.get("trust") or "runtime"),
                },
            )
        )
    return refs


def _skill_refs(skill_ids: Optional[List[str]]) -> List[AssetRef]:
    if not skill_ids:
        return []
    from core.agent_skills.loader import get_skill_loader

    metadata: Dict[str, Any] = {}
    try:
        metadata = get_skill_loader().load_all_metadata() or {}
    except Exception:
        metadata = {}

    refs: List[AssetRef] = []
    for skill_id in skill_ids:
        item = metadata.get(skill_id) or {}
        version = ""
        source = ""
        if isinstance(item, dict):
            version = str(item.get("version") or "")
            source = str(item.get("source") or "")
        else:  # loader may hand back an object
            version = str(getattr(item, "version", "") or "")
            source = str(getattr(item, "source", "") or "")
        refs.append(
            AssetRef(
                kind=ASSET_SKILL,
                asset_id=skill_id,
                # Filesystem skills have no version string; "fs" keeps the ref
                # meaningful instead of pretending we know a version.
                version=version or ("fs" if source == "filesystem" else "unversioned"),
                detail={"source": source} if source else {},
            )
        )
    return refs


def _ontology_refs() -> List[AssetRef]:
    from core.db.engine import SessionLocal
    from core.db.models import OntologyPack

    refs: List[AssetRef] = []
    with SessionLocal() as db:
        packs = db.query(OntologyPack).all()
        for pack in packs:
            active = getattr(pack, "active_version_id", None)
            if not active:
                continue
            refs.append(
                AssetRef(
                    kind=ASSET_ONTOLOGY,
                    asset_id=str(pack.pack_id),
                    version=str(active),
                )
            )
    return refs


def _memory_refs(memory_enabled: bool, workspace_id: str) -> List[AssetRef]:
    from core.config.settings import settings

    # The memory *policy* is what evolves; the memory contents are evidence, not
    # a pinned version. Recording the policy knobs is what lets a later replay
    # reproduce the same retrieval behaviour. Per-layer, because attribution
    # needs to distinguish "L3 offered nothing" from "L3 was switched off" —
    # a single opaque flag cannot answer which policy a given Episode ran under.
    effective = bool(memory_enabled and settings.memory.enabled)
    return [
        AssetRef(
            kind=ASSET_MEMORY,
            asset_id=f"policy:{workspace_id or 'default'}",
            version="v2",
            detail={
                "enabled": effective,
                "retrieval_budget_ms": settings.memory.retrieval_budget_ms,
                "layers": {
                    "profile": {"enabled": effective},
                    "fact": {
                        "enabled": effective,
                        "layered": bool(settings.memory.layered_enabled),
                        "dedup_min_score": settings.memory.procedure_dedup_min_score,
                    },
                    "graph": {
                        "enabled": bool(effective and settings.memory.graph_enabled),
                    },
                },
            },
        )
    ]


def _model_refs(model_name: Optional[str], provider_id: Optional[str]) -> List[AssetRef]:
    if not model_name and not provider_id:
        return []
    return [
        AssetRef(
            kind=ASSET_MODEL,
            asset_id=str(provider_id or "default"),
            version=str(model_name or ""),
        )
    ]


def _kb_refs(kb_ids: Optional[List[str]]) -> List[AssetRef]:
    return [AssetRef(kind=ASSET_KB, asset_id=str(kb_id), version="") for kb_id in (kb_ids or [])]


def _workflow_refs(
    chat_mode: Optional[str],
    policy_version: Optional[str],
    profile_id: Optional[str] = None,
) -> List[AssetRef]:
    """Pin the orchestration profile this run was assembled from.

    The profile decides which tools were visible, which skills were offered and
    what the budgets were, so a bundle that does not name it cannot reproduce
    the run — and counterfactual replay, which holds everything fixed while
    substituting one asset, would be substituting against an unknown baseline.
    """
    return [
        AssetRef(
            kind=ASSET_WORKFLOW,
            asset_id=f"profile:{profile_id or 'builtin'}",
            version=policy_version or "builtin",
            detail={"chat_mode": chat_mode or "default"},
        )
    ]


def bind_runtime_assets(
    *,
    run_id: Optional[str] = None,
    skill_ids: Optional[List[str]] = None,
    kb_ids: Optional[List[str]] = None,
    model_name: Optional[str] = None,
    model_provider_id: Optional[str] = None,
    chat_mode: Optional[str] = None,
    memory_enabled: bool = False,
    workspace_id: str = "default",
    workflow_policy_version: Optional[str] = None,
    orchestration_profile_id: Optional[str] = None,
    execution_manifest: Optional[Any] = None,
    manifest_required: bool = False,
) -> AssetBundle:
    """Resolve and pin everything this run depends on.

    Always returns a bundle.  A resolver that fails marks the bundle partial
    rather than raising: the caller is on the user's critical path.
    """
    degraded: List[str] = []
    refs: List[AssetRef] = []

    try:
        if execution_manifest is None:
            manifest_payload: Dict[str, Any] = {}
        elif hasattr(execution_manifest, "to_dict"):
            manifest_payload = dict(execution_manifest.to_dict())
        elif isinstance(execution_manifest, dict):
            manifest_payload = dict(execution_manifest)
        else:
            manifest_payload = {}
    except Exception as exc:  # evidence conversion must not fail the turn
        logger.debug("[binder] execution manifest conversion failed: %s", exc)
        manifest_payload = {}
        degraded.append("execution_manifest")

    exact_prompt_refs = _prompt_refs_from_manifest(manifest_payload)
    if exact_prompt_refs:
        refs += exact_prompt_refs
    else:
        refs += _safe(lambda: _prompt_refs(), "prompt", degraded)
        if manifest_required and "execution_manifest" not in degraded:
            degraded.append("execution_manifest")
    refs += _safe(lambda: _skill_refs(skill_ids), "skill", degraded)
    refs += _safe(lambda: _ontology_refs(), "ontology", degraded)
    refs += _safe(lambda: _memory_refs(memory_enabled, workspace_id), "memory", degraded)
    refs += _safe(lambda: _model_refs(model_name, model_provider_id), "model", degraded)
    refs += _safe(lambda: _kb_refs(kb_ids), "kb", degraded)
    refs += _safe(
        lambda: _workflow_refs(chat_mode, workflow_policy_version, orchestration_profile_id),
        "workflow",
        degraded,
    )

    bundle = build_bundle(
        refs,
        partial=bool(degraded),
        captured_at=datetime.now(timezone.utc).isoformat(),
        execution_manifest=manifest_payload,
    )
    if degraded:
        logger.info("[binder] partial bundle %s (degraded: %s)", bundle.bundle_id, degraded)
    _remember(bundle, run_id)
    return bundle


def rebind_execution_manifest(
    *,
    run_id: Optional[str],
    base_bundle: AssetBundle,
    execution_manifest: Any,
) -> AssetBundle:
    """Replace request evidence without re-resolving the run's frozen assets.

    Context manifests change per model request, while governed asset versions
    must remain pinned for the run. Calling ``bind_runtime_assets`` again here
    would reopen a publish race and could attach newly activated versions that
    the running Agent never consumed.
    """
    if base_bundle is None:
        raise ValueError("base bundle is required for manifest-only rebind")
    if hasattr(execution_manifest, "to_dict"):
        manifest_payload = dict(execution_manifest.to_dict())
    elif isinstance(execution_manifest, dict):
        manifest_payload = dict(execution_manifest)
    else:
        raise TypeError("execution manifest must be serializable")
    bundle = build_bundle(
        list(base_bundle.refs),
        partial=base_bundle.partial,
        captured_at=base_bundle.captured_at,
        execution_manifest=manifest_payload,
    )
    _remember(bundle, run_id)
    return bundle


def reset_for_tests() -> None:
    with _lock:
        _bundles.clear()
        _run_to_bundle.clear()
