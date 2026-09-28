"""Explicit device-to-cloud private skill registration with durable retry identity."""
import hashlib
from core.capabilities import registry, store, archive
from core.capabilities.paths import LOCAL_PROFILE
from core.agent_skills.binary_files import encode_upload
from core.services.management_package import serialized
from core.services.local_skill_service import _owned


@serialized
def upload(user_id, install_id, expected_revision, expected_cloud_revision=None):
    from core.capabilities import skills
    from core.capabilities.change_sync import cloud_request
    from core.services import desktop_cloud_bridge as bridge
    state = bridge.get_state()
    bridge.require_current_account(state)
    if not skills.account_authorized_for(user_id):
        raise PermissionError("current cloud account does not own this local session")
    inst = _owned(user_id, install_id)
    if not expected_revision or inst.resolved_revision != expected_revision:
        raise ValueError("revision_conflict")
    profile = skills.current_account_profile()
    links = dict(inst.payload.get("cloud_uploads") or {})
    link = dict(links.get(profile) or {})
    comp = store.get("skill", LOCAL_PROFILE, inst.key, expected_revision)
    if comp is None:
        raise ValueError("installed skill files missing")
    files = {n: encode_upload(n, p.read_bytes()) for n, p in archive.iter_files(comp.path)
             if n != ".inventory.json"}
    target = link.get("key") or ("device-" + hashlib.sha256((profile + install_id).encode()).hexdigest()[:32])
    # Rewrite identity once to the stable cloud destination, never derive a target from a display name.
    from core.services.marketplace_service import _rewrite_frontmatter_name
    files["SKILL.md"] = _rewrite_frontmatter_name(files["SKILL.md"], target)
    identity = hashlib.sha256((profile + install_id + expected_revision + str(expected_cloud_revision)).encode()).hexdigest()
    if link.get("local_revision") == expected_revision and link.get("revision"):
        remote = cloud_request(state, "GET", "skill", target)
        if remote["revision"] == link["revision"]:
            return {"ok": True, "install_id": install_id, "local_revision": expected_revision,
                    "cloud_skill_id": target, "cloud_revision": link["revision"],
                    "source": "cloud", "action": "uploaded_private_skill"}
    pending = link.get("pending")
    if not pending or pending.get("request_id") != identity:
        remote = cloud_request(state, "GET", "skill", target)
        if link.get("revision"):
            if not expected_cloud_revision or expected_cloud_revision != remote["revision"]:
                raise ValueError("cloud_revision_conflict: refresh cloud revision before uploading")
        elif remote.get("exists"):
            raise ValueError("cloud_destination_exists: refusing to overwrite unrelated content")
        pending = {"request_id": identity, "expected_revision": remote["revision"],
                   "expected_model_version": remote.get("model_version"), "create_only": not remote.get("exists")}
        links[profile] = {**link, "key": target, "pending": pending}
        with bridge.account_scope(state):
            registry.set_state(install_id, inst.state, payload_update={"cloud_uploads": links})
    body = {**pending, "files": files}
    response = cloud_request(state, "POST", "skill", target, body)
    if not response.get("applied"):
        raise ValueError("cloud saved package but did not register it")
    with bridge.account_scope(state):
        links[profile] = {"key": target, "revision": response["revision"], "local_revision": expected_revision}
        registry.set_state(install_id, inst.state, payload_update={"cloud_uploads": links})
    return {"ok": True, "install_id": install_id, "local_revision": expected_revision,
            "cloud_skill_id": target, "cloud_revision": response["revision"],
            "source": "cloud", "action": "uploaded_private_skill"}
