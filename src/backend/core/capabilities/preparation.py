"""Account-bound publication and recovery shared by the three file package kinds."""

from __future__ import annotations

import threading

from . import registry, store
from .errors import CloudUnavailable, IntegrityFailed, PackageMissing
from .paths import revision_for_hash

_prepare_lock = threading.RLock()


def prepare_component(state, inst, *, download, content_hash, after_publish=None):
    from core.services.desktop_cloud_bridge import account_scope, require_current_account
    from core.services.desktop_cloud_skills import _profile

    if inst.profile_id != _profile(state):
        raise PackageMissing("installation belongs to another account", ref=inst.install_id)
    if not inst.content_hash:
        raise IntegrityFailed("cloud manifest published no content hash", ref=inst.install_id)
    revision = revision_for_hash(inst.content_hash)
    old_revision = inst.resolved_revision
    old_ready = False
    tx = None
    try:
        with _prepare_lock:
            require_current_account(state)
            old_ready = bool(
                inst.ready
                and old_revision
                and store.get(inst.kind, inst.profile_id, inst.key, old_revision)
            )
            with account_scope(state):
                comp = store.get(inst.kind, inst.profile_id, inst.key, revision)
                if comp is None:
                    registry.set_state(inst.install_id, "preparing", resolved_revision=old_revision)
                    tx = registry.begin_transaction(inst.install_id, inst.generation + 1)
        # Fetched outside _prepare_lock on purpose. A name lookup can block long
        # past the HTTP timeout, and holding the lock across it stalls every other
        # preparation — including the on-demand one the chat assembly waits on, so
        # one unreachable cloud used to freeze the whole conversation. Releasing
        # the lock is safe because the commit below re-validates the installation.
        data = download() if comp is None else None
        with _prepare_lock:
            # A login can proceed during HTTP IO. No stale bytes/intent may be
            # published after it; the short commit below shares its identity lock.
            with account_scope(state):
                current = registry.get(inst.install_id)
                if (
                    current is None
                    or current.state == "removed"
                    or current.content_hash != inst.content_hash
                ):
                    raise CloudUnavailable("installation changed while preparing; retry")
                if comp is None:
                    comp = store.write_from_zip(
                        inst.kind, inst.profile_id, inst.key, revision, data
                    )
                actual = content_hash(comp)
                if actual != inst.content_hash:
                    if old_revision == revision:
                        old_ready = False
                    store.remove_revision(inst.kind, inst.profile_id, inst.key, revision)
                    raise IntegrityFailed(
                        "stored content does not match the published hash",
                        ref=inst.install_id,
                        details={"expected": inst.content_hash, "actual": actual},
                    )
                if tx:
                    registry.advance_transaction(tx, "published", inventory=store.inventory(comp))
                if after_publish:
                    after_publish(comp)
                done = registry.set_state(
                    inst.install_id,
                    "ready",
                    resolved_revision=revision,
                    payload_update={"resolved_content_hash": inst.content_hash},
                )
                if tx:
                    registry.advance_transaction(tx, "committed")
                # Interrupted earlier attempts for the same verified generation
                # can now be reconciled safely; existence alone never proves it.
                for open_tx in registry.open_transactions():
                    if open_tx["install_id"] == inst.install_id:
                        registry.advance_transaction(open_tx["tx_id"], "committed")
                return done
    except Exception as exc:
        with _prepare_lock:
            if tx:
                registry.advance_transaction(tx, "failed", error=str(exc))
            # Preserve the prior version on transport/update failure. Its content
            # remains available to already frozen runs, including after restart.
            current = registry.get(inst.install_id)
            if current and current.content_hash == inst.content_hash and current.state != "removed":
                registry.set_state(
                    inst.install_id,
                    "ready" if old_ready else "failed",
                    resolved_revision=old_revision,
                    last_error=str(exc),
                    payload_update={"update_available": True} if old_ready else None,
                )
        raise


def ensure_cloud_ready(user_id, *, skill_keys=(), plugin_keys=(), install_ids=()):
    """按需准备当前账号的云端能力：未就绪的技能 / 插件 / 智能体先下载发布，调用方再重新解析。

    云端是能力真源，本机只缓存用过的组件；用户在对话里选中一个尚未下载的云端技能或插件
    时，这里就是"按需拉取"的落点。桥未激活、没有对应的云端安装记录、或组件已就绪时不做
    任何事。返回准备失败的 install_id 列表（下载/校验失败），由调用方决定如何报错。
    """
    from . import registry, skills
    from core.services import desktop_cloud_bridge as bridge

    if not skills.account_authorized_for(user_id):
        return []
    state = bridge.get_state()
    profile = skills.current_account_profile()
    if not state or not profile:
        return []

    wanted = {str(iid) for iid in install_ids if iid}
    for key in skill_keys:
        wanted.add(registry.install_id("skill", profile, str(key)))
    for key in plugin_keys:
        wanted.add(registry.install_id("plugin", profile, str(key)))
    # 插件 / 智能体定义的组件也要一起准备（定义先就绪才知道组件）。
    for owner in list(wanted):
        wanted.update(registry.components_of(owner))

    def pending(kind_filter):
        rows = []
        for iid in sorted(wanted):
            inst = registry.get(iid)
            if inst is None or inst.profile_id != profile or inst.state == "removed":
                continue
            if inst.kind in kind_filter and inst.enabled and not inst.ready:
                rows.append(iid)
        return rows

    failures = []
    definitions = pending(("plugin", "agent"))
    if definitions:
        from core.services import desktop_cloud_bundles

        failures += [r["install_id"] for r in desktop_cloud_bundles.prepare(state, definitions) if not r["ok"]]
        for owner in definitions:
            wanted.update(registry.components_of(owner))
    skill_ids = pending(("skill",))
    if skill_ids:
        from core.services import desktop_cloud_skills

        failures += [r["install_id"] for r in desktop_cloud_skills.prepare(state, skill_ids) if not r["ok"]]
    return failures

