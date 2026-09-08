"""桌面双端：云端智能体 / 插件定义的清单同步与落盘。

与技能同一轮询、同一枚 capability token。两类都是纯定义（agent.json +
instructions.md / plugin.json），没有脚本、没有二进制、没有运行依赖，所以清单
一到就直接准备：下载定义包 → 按清单哈希核对 → 发布成不可变 revision →
写安装索引；插件另记组件归属边（技能 / 连接器的 install_id），就绪状态由
组件推导，不凭 plugin.json 存在就宣布就绪。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from core.capabilities import plugins, registry, store, manifest_order
from core.capabilities.errors import CloudUnavailable, IntegrityFailed
from core.capabilities.paths import KIND_AGENT, KIND_PLUGIN, capabilities_enabled, revision_for_hash
from core.capabilities.ref import cloud_ref, profile_id
from core.services.desktop_capability_protocol import entity_content_hash, entity_id_key

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_manifests: Dict[str, Optional[Dict[str, Any]]] = {KIND_AGENT: None, KIND_PLUGIN: None}
_errors: Dict[str, Optional[str]] = {KIND_AGENT: None, KIND_PLUGIN: None}

_PATHS = {KIND_AGENT: "agents", KIND_PLUGIN: "plugins"}


def _profile(state: Dict[str, Any]) -> str:
    from core.services.desktop_capability_protocol import token_subject

    subject = token_subject(str(state.get("token") or ""))
    if not subject:
        raise CloudUnavailable("capability token carries no subject; cannot map an account profile")
    return profile_id(str(state["cloud_base"]), subject)


def _headers(state: Dict[str, Any]) -> Dict[str, str]:
    from core.services.desktop_cloud_bridge import cloud_headers

    return cloud_headers(state)


def _fetch(kind: str, state: Dict[str, Any]) -> Dict[str, Any]:
    import httpx
    from core.services.desktop_capability_protocol import validate_entity_manifest

    ticket = manifest_order.begin(kind, _profile(state))
    with _lock:
        current = _manifests.get(kind)
    headers = _headers(state)
    if current:
        headers["If-None-Match"] = f'"{current["revision"]}"'
    resp = httpx.get(
        f"{state['cloud_base']}/api/v1/desktop/capability/{_PATHS[kind]}/manifest",
        headers=headers,
        timeout=httpx.Timeout(10.0, connect=5.0),
    )
    if resp.status_code == 304 and current:
        return manifest_order.stamp(current, ticket)
    resp.raise_for_status()
    body = resp.json()
    return manifest_order.stamp(
        validate_entity_manifest(body.get("data") if isinstance(body, dict) else None, kind), ticket
    )


def _download(kind: str, state: Dict[str, Any], ident: str) -> bytes:
    import httpx

    try:
        resp = httpx.get(
            f"{state['cloud_base']}/api/v1/desktop/capability/{_PATHS[kind]}/{ident}/bundle",
            headers=_headers(state),
            timeout=httpx.Timeout(30.0, connect=5.0),
        )
    except httpx.HTTPError as exc:
        raise CloudUnavailable(f"{kind} bundle download failed: {exc}", ref=ident) from exc
    if resp.status_code >= 400:
        raise CloudUnavailable(f"{kind} bundle download HTTP {resp.status_code}", ref=ident)
    return resp.content


def _dir_hash(comp: store.StoredComponent) -> str:
    files = {rel: path.read_text(encoding="utf-8") for rel, path in _text_files(comp)}
    return entity_content_hash(files)


def _text_files(comp: store.StoredComponent):
    from core.capabilities import archive

    for rel, path in archive.iter_files(comp.path):
        if rel == ".inventory.json":
            continue
        yield rel, path


def _prepare(kind: str, state: Dict[str, Any], inst: registry.Installation) -> None:
    from core.capabilities.preparation import prepare_component

    remote_id = str(inst.payload.get("cloud_install_id") or inst.key)

    def after_publish(comp):
        if kind == KIND_PLUGIN:
            registry.set_components(
                inst.install_id,
                plugins.component_install_ids(inst.profile_id, plugins.load_manifest(comp)),
            )

    prepare_component(
        state,
        inst,
        download=lambda: _download(kind, state, remote_id),
        content_hash=_dir_hash,
        after_publish=after_publish,
    )


def prepare(state: Dict[str, Any], install_ids: List[str]) -> List[Dict[str, Any]]:
    """Prepare selected agent/plugin definitions using the same verified publication."""
    from core.capabilities.errors import PackageMissing

    results = []
    for iid in install_ids:
        try:
            inst = registry.get(iid)
            if inst is None or inst.kind not in (KIND_AGENT, KIND_PLUGIN):
                raise PackageMissing("unknown definition installation", ref=iid)
            _prepare(inst.kind, state, inst)
            results.append(
                {"install_id": iid, "ok": True, "installation": registry.get(iid).to_dict()}
            )
        except Exception as exc:
            error = (
                exc.to_dict()
                if hasattr(exc, "to_dict")
                else {"code": "prepare_failed", "message": str(exc)}
            )
            results.append({"install_id": iid, "ok": False, "error": error})
    return results


def _reconcile(
    kind: str, manifest: Dict[str, Any], state: Dict[str, Any], *, prepare: bool = True
) -> int:
    """Publish all current intentions before any optional package download."""
    from core.services.desktop_cloud_bridge import account_scope

    profile = _profile(state)
    with account_scope(state):
        with manifest_order.apply(kind, profile, manifest):
            candidates = _apply_intent(kind, manifest, state)
            with _lock:
                _manifests[kind], _errors[kind] = manifest, None
    prepared = 0
    if prepare:
        for inst in candidates:
            # Never hold the account/ordering gate during download. Publication
            # still verifies the current intention hash in prepare_component.
            with account_scope(state):
                with manifest_order.apply(kind, profile, manifest):
                    pass
            try:
                _prepare(kind, state, inst)
                prepared += 1
            except Exception as exc:
                logger.warning("[cloud-%s] prepare '%s' failed: %s", kind, inst.key, exc)
    return prepared


def _apply_intent(
    kind: str, manifest: Dict[str, Any], state: Dict[str, Any]
) -> List[registry.Installation]:
    profile = _profile(state)
    cloud_base = str(state["cloud_base"])
    id_key = entity_id_key(kind)
    wanted = {str(e["slug"] if kind == KIND_PLUGIN else e[id_key]): e for e in manifest["entries"]}
    existing = {
        inst.key: inst
        for inst in registry.list_installations(kind=kind, profile_id=profile, include_removed=True)
    }
    candidates = []
    for ident, entry in wanted.items():
        enabled = bool(entry.get("is_enabled", entry.get("enabled", True)))
        payload: Dict[str, Any] = {}
        if kind == KIND_PLUGIN:
            payload = {
                "cloud_install_id": str(entry[id_key]),
                "components": {
                    key: entry.get(key, []) for key in ("skills", "mcp", "agents", "plugins")
                },
            }
        inst = registry.upsert(
            profile_id=profile,
            ref=cloud_ref(cloud_base, kind, ident, scope="shared"),
            display_name=str(entry.get("name") or ident),
            description=str(entry.get("description") or ""),
            version=str(entry.get("version") or ""),
            content_hash=str(entry["content_hash"]),
            source="cloud",
            payload=payload,
            enabled=enabled,
        )
        if not (
            inst.ready and inst.resolved_revision == revision_for_hash(inst.content_hash or "")
        ):
            candidates.append(inst)
    for ident, inst in existing.items():
        if ident not in wanted and inst.state != "removed":
            registry.mark_removed(inst.install_id)
    return candidates


def sync_kind(kind: str, state: Dict[str, Any]) -> bool:
    """Fetch + reconcile one kind; returns whether anything changed."""
    if not capabilities_enabled():
        with _lock:
            _errors[kind] = "capability store disabled (HUGAGENT_CAPS_ROOT unset)"
        return False
    try:
        from core.services.desktop_cloud_bridge import account_scope

        manifest = _fetch(kind, state)
        with _lock:
            changed = (
                _manifests[kind] is None or _manifests[kind]["revision"] != manifest["revision"]
            )
        prepared = _reconcile(kind, manifest, state)
    except manifest_order.StaleManifest:
        return False
    except Exception as exc:  # noqa: BLE001
        from core.services.desktop_cloud_bridge import _state_fingerprint, get_state

        if _state_fingerprint(get_state()) != _state_fingerprint(state):
            return False
        with _lock:
            _errors[kind] = str(exc)
        logger.warning("[cloud-%s] sync failed: %s", kind, exc)
        return False
    if changed or prepared:
        logger.info(
            "[cloud-%s] synced revision=%s entries=%d prepared=%d",
            kind,
            manifest["revision"][:12],
            len(manifest["entries"]),
            prepared,
        )
    return changed or bool(prepared)


def sync_blocking(state: Dict[str, Any]) -> None:
    changed = False
    for kind in (KIND_AGENT, KIND_PLUGIN):
        changed = sync_kind(kind, state) or changed
    if changed:
        from core.config.catalog_resolver import invalidate_capability_cache

        invalidate_capability_cache()


def on_account_switch() -> None:
    with _lock:
        for kind in _manifests:
            _manifests[kind] = None
            _errors[kind] = None


def status() -> Dict[str, Any]:
    from core.services.desktop_cloud_bridge import get_state

    st = get_state()
    out: Dict[str, Any] = {}
    for kind in (KIND_AGENT, KIND_PLUGIN):
        with _lock:
            manifest = _manifests.get(kind)
            err = _errors.get(kind)
        rows: List[Dict[str, Any]] = []
        if st and capabilities_enabled():
            try:
                rows = [
                    i.to_dict()
                    for i in registry.list_installations(kind=kind, profile_id=_profile(st))
                ]
            except CloudUnavailable as exc:
                err = err or str(exc)
        out[kind] = {
            "revision": str((manifest or {}).get("revision") or ""),
            "cloud_count": len((manifest or {}).get("entries") or []),
            "installed_count": sum(1 for r in rows if r["state"] == "ready"),
            "installations": rows,
            "last_error": err,
        }
    return out


def reset_for_tests() -> None:  # pragma: no cover
    with _lock:
        for kind in _manifests:
            _manifests[kind] = None
            _errors[kind] = None
