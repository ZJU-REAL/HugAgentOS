"""Explicit device-to-cloud private skill registration with durable retry identity."""
from features.desktop.bridge import identity as bridge_identity
from features.desktop.capabilities import skill_account
import hashlib
from features.desktop.capabilities import registry
from features.desktop.capabilities import store
from core.capabilities import archive as archive
from core.capabilities.identity_rules import LOCAL_PROFILE
from core.capabilities.skills.binary_files import encode_upload
from core.capabilities.plugins.management.package import serialized
from features.desktop.local.skills.service import _owned


@serialized
def upload(user_id, install_id, expected_revision, expected_cloud_revision=None):
    from features.desktop.capabilities import skills
    from features.desktop.capabilities.change_sync import cloud_request
    from features.desktop.bridge import client as bridge
    state = bridge_identity.get_state()
    bridge_identity.require_current_account(state)
    if not skill_account.account_authorized_for(user_id):
        raise PermissionError("current cloud account does not own this local session")
    inst = _owned(user_id, install_id)
    if not expected_revision or inst.resolved_revision != expected_revision:
        raise ValueError("revision_conflict")
    profile = skill_account.current_account_profile()
    links = dict(inst.payload.get("cloud_uploads") or {})
    link = dict(links.get(profile) or {})
    comp = store.get("skill", LOCAL_PROFILE, inst.key, expected_revision)
    if comp is None:
        raise ValueError("installed skill files missing")
    files = {n: encode_upload(n, p.read_bytes()) for n, p in archive.iter_files(comp.path)
             if n != ".inventory.json"}
    target = link.get("key") or ("device-" + hashlib.sha256((profile + install_id).encode()).hexdigest()[:32])
    # Rewrite identity once to the stable cloud destination, never derive a target from a display name.
    from core.capabilities.skills.installation import rewrite_frontmatter_name as _rewrite_frontmatter_name
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
        with bridge_identity.account_scope(state):
            registry.set_state(install_id, inst.state, payload_update={"cloud_uploads": links})
    body = {**pending, "files": files}
    response = cloud_request(state, "POST", "skill", target, body)
    if not response.get("applied"):
        raise ValueError("cloud saved package but did not register it")
    with bridge_identity.account_scope(state):
        links[profile] = {"key": target, "revision": response["revision"], "local_revision": expected_revision}
        registry.set_state(install_id, inst.state, payload_update={"cloud_uploads": links})
    return {"ok": True, "install_id": install_id, "local_revision": expected_revision,
            "cloud_skill_id": target, "cloud_revision": response["revision"],
            "source": "cloud", "action": "uploaded_private_skill"}
