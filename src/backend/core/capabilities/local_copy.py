"""Create an independent editable local skill without mutating its cloud source."""

from __future__ import annotations

import uuid
import threading
from . import archive, registry, skills, store
from .errors import IntegrityFailed, PackageMissing, InstallConflict
from .paths import LOCAL_PROFILE, safe_segment


_edit_lock = threading.RLock()


def _content_hash(files):
    from core.agent_skills.binary_files import encode_upload
    from core.services.desktop_capability_protocol import skill_content_hash

    encoded = {name: encode_upload(name, data) for name, data in files.items()}
    return skill_content_hash(encoded.pop("SKILL.md", ""), encoded)


def create_skill_copy(inst, *, user_id, state, runtime_name=None):
    from core.services.desktop_cloud_bridge import account_scope

    if inst.kind != "skill" or inst.profile_id == LOCAL_PROFILE:
        raise ValueError("only prepared cloud skills support local copies")
    name = safe_segment(runtime_name or inst.payload.get("runtime_name") or inst.key)
    with account_scope(state):
        if not inst.ready:
            raise PackageMissing(
                "prepare the cloud skill before creating a local copy", ref=inst.install_id
            )
        comp = store.get("skill", inst.profile_id, inst.key, inst.resolved_revision)
        if comp is None:
            raise PackageMissing("cloud skill files are missing", ref=inst.install_id)
        # Read only ordinary content, verify the current ready revision rather
        # than the latest advertised update, and never carry links into copies.
        files = {
            rel: path.read_bytes()
            for rel, path in archive.iter_files(comp.path)
            if rel != ".inventory.json"
        }
        expected = inst.payload.get("resolved_content_hash") or inst.content_hash
        digest = _content_hash(files)
        if not expected or digest != expected:
            raise IntegrityFailed(
                "cloud skill content changed; prepare it again", ref=inst.install_id
            )
        key = "copy-" + uuid.uuid4().hex
        local = skills.publish_local_skill(
            key,
            files=files,
            content_hash=digest,
            owner_user_id=user_id,
            display_name=inst.display_name,
            description=inst.description,
            version=inst.version,
        )
        iid = registry.install_id("skill", LOCAL_PROFILE, local.key)
        registry.set_state(
            iid,
            "ready",
            resolved_revision=local.revision,
            payload_update={
                "runtime_name": name,
                "derived_from": inst.install_id,
                "derived_resource_ref": inst.ref.to_dict(),
                "derived_revision": inst.resolved_revision,
            },
        )
        registry.set_preference("skill", name, iid, chosen_by=user_id)
        skills.bump_view_generation()
        return registry.get(iid)


def editable_component(inst, user_id):
    if (
        inst.kind != "skill"
        or inst.profile_id != LOCAL_PROFILE
        or inst.payload.get("from_db")
        or inst.payload.get("owner_user_id") != user_id
    ):
        raise ValueError("only your local skill copies can be edited here")
    comp = (
        store.get("skill", LOCAL_PROFILE, inst.key, inst.resolved_revision or "")
        if inst.ready
        else None
    )
    if comp is None:
        raise PackageMissing("local skill files are missing", ref=inst.install_id)
    return comp


def read_file(inst, *, user_id, filename):
    comp = editable_component(inst, user_id)
    filename = archive._normalize_member(filename)
    if filename == ".inventory.json":
        raise ValueError("reserved package metadata")
    files = dict(archive.iter_files(comp.path))
    path = files.get(filename)
    if path is None:
        raise FileNotFoundError(filename)
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("text editor supports files up to 1 MiB")
    data = path.read_bytes()
    try:
        content = data.decode("utf-8")
        binary = "\x00" in content
    except UnicodeDecodeError:
        content, binary = "", True
    return {
        "filename": filename,
        "content": "" if binary else content,
        "is_binary": binary,
        "revision": inst.resolved_revision,
    }


def update_file(inst, *, user_id, filename, content, expected_revision):
    with _edit_lock:
        inst = registry.get(inst.install_id)
        if not inst or inst.resolved_revision != expected_revision:
            raise InstallConflict("skill changed since it was opened; reload before saving")
        comp = editable_component(inst, user_id)
        filename = archive._normalize_member(filename)
        if filename == ".inventory.json":
            raise ValueError("reserved package metadata")
        if len(content.encode("utf-8")) > 1024 * 1024 or "\x00" in content:
            raise ValueError("only UTF-8 text up to 1 MiB is supported")
        files = {
            name: path.read_bytes()
            for name, path in archive.iter_files(comp.path)
            if name != ".inventory.json"
        }
        expected_hash = inst.payload.get("resolved_content_hash") or inst.content_hash
        if _content_hash(files) != expected_hash:
            raise IntegrityFailed(
                "local skill content was changed outside the editor; reload a verified revision"
            )
        from core.agent_skills.registry import _load_skill_from_str

        name = str(inst.payload.get("runtime_name") or inst.key)
        # A chosen runtime alias is separate from the copied frontmatter ID.
        # Preserve the existing file identity without requiring it to equal the alias.
        previous = _load_skill_from_str(files["SKILL.md"].decode("utf-8"), name)
        files[filename] = content.encode("utf-8")
        parsed = _load_skill_from_str(files["SKILL.md"].decode("utf-8"), name)
        if parsed.id != previous.id:
            raise ValueError("keep the skill frontmatter name unchanged")
        skills.publish_local_skill(
            inst.key,
            files=files,
            content_hash=_content_hash(files),
            owner_user_id=user_id,
            display_name=parsed.name,
            description=parsed.description,
            version=parsed.version,
            enabled=inst.enabled,
        )
        return registry.get(inst.install_id)
