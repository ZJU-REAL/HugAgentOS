"""Workspace persona files (OpenClaw style) and the Personal Agent system prompt.

The agent's persona, user model and memory live as Markdown files at the root of its
permanent workspace. They are injected into every task prompt, below the platform
rules, which always take precedence. Platform rules and the files' seed templates are
prompt parts, edited in the Config console. The backend reads the host side of the workspace
volume and never follows a link: the task user owns these files.
"""

from personal_agent.workspace.constants import BOOTSTRAP_FILE
from personal_agent.workspace.constants import DEFAULT_NAME
from personal_agent.workspace.constants import PROFILE_FILES
from personal_agent.workspace.constants import TEMPLATE_PARTS

from personal_agent.workspace.files import WorkspaceUnavailable
from personal_agent.workspace.files import _open_dir
from personal_agent.workspace.files import _own
from personal_agent.workspace.files import _raw
from personal_agent.workspace.files import etag
from personal_agent.workspace.files import local_workspace
from personal_agent.workspace.prompting import prompt_part
from personal_agent.workspace.files import read_file
from personal_agent.workspace.files import write_file

import os

# Default agent name, after HugAgentOS. The user can rename it in settings or in conversation.

# Injection order, as in OpenClaw: operating manual, persona, identity, user, first run, memory.

# Prompt bodies live in the prompt version pool (Config console -> prompt management,
# kind "personal_agent"); prompts/*.system.md seeds it and is the fallback.
# Seed template for each workspace file, as a part of the prompt kind.
# Read only in a heartbeat turn (heartbeat.py), not injected into every prompt.
# The first run is over once one of these differs from what was seeded (as in OpenClaw).

def seed(ident, name=None):
    """Create missing default files once per workspace. Returns True when files were written.

    BOOTSTRAP.md is only written into a brand-new workspace, like OpenClaw's first-run ritual.
    Existing files are never overwritten.
    """
    root = local_workspace(ident)
    root.mkdir(parents=True, exist_ok=True)
    try: os.chmod(root, 0o700)
    except OSError: pass
    fd = _open_dir(root, [])
    try:
        def exists(rel):
            try: os.stat(rel, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError: return False
            return True
        fresh = not any(exists(rel) for rel in ("AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md", "memory"))
        wrote = False
        for rel, part_id in TEMPLATE_PARTS.items():
            if rel == "BOOTSTRAP.md" and not fresh: continue
            if exists(rel): continue
            body = prompt_part(part_id).replace("{name}", name or DEFAULT_NAME) + "\n"
            out = os.open(rel, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            with os.fdopen(out, "wb") as handle: handle.write(body.encode("utf-8"))
            _own(rel, fd)
            wrote = True
        return wrote
    finally:
        os.close(fd)

def ensure_seeded(ident):
    """Seed once; the flag keeps a deliberately deleted file from coming back."""
    from core.db.engine import SessionLocal
    from personal_agent.models import PersonalAgent
    with SessionLocal() as db:
        row = db.get(PersonalAgent, ident)
        if row is None or (row.config or {}).get("workspace_seeded"): return False
        name = (row.config or {}).get("name")
    seed(ident, name)
    # What the profile files looked like when seeded: a change to one of them ends the first run.
    hashes = {rel: etag(data) for rel in PROFILE_FILES if (data := _raw(ident, rel)) is not None}
    with SessionLocal() as db:
        row = db.query(PersonalAgent).filter(PersonalAgent.id == ident).with_for_update().one()
        row.config = {**(row.config or {}), "workspace_seeded": True, "seed_hashes": hashes}
        db.commit()
    return True

def _configured(ident, config):
    """Only profile changes finish onboarding; automatic journals do not."""
    seeded = config.get("seed_hashes") or {}
    for rel in PROFILE_FILES:
        data = _raw(ident, rel)
        if data is None: continue
        # A workspace seeded before the hashes were kept compares with the template.
        expected = seeded.get(rel) or etag((prompt_part(TEMPLATE_PARTS[rel]).replace("{name}", config.get("name") or DEFAULT_NAME) + "\n").encode("utf-8"))
        if etag(data) != expected: return True
    return False

def settle_bootstrap(ident, config):
    """True while the first run is pending. Like OpenClaw, the platform removes BOOTSTRAP.md once
    the workspace is configured; the agent never has to delete it. config is the agent's config."""
    root = local_workspace(ident)
    if _raw(ident, BOOTSTRAP_FILE) is None: return False
    if not _configured(ident, config or {}): return True
    fd = _open_dir(root, [])
    try: os.unlink(BOOTSTRAP_FILE, dir_fd=fd)
    except FileNotFoundError: pass
    finally: os.close(fd)
    return False

def _is_name_line(line):
    return line.lstrip().startswith("- **名字：**") or line.lstrip().lower().startswith("- **name:**")

def identity_name(content):
    """The name on the name line of IDENTITY.md, or None when there is no usable one."""
    for line in (content or "").split("\n"):
        if _is_name_line(line):
            value = line[line.index("**", line.index("**") + 2) + 2:].strip()
            # A placeholder such as "_(...)_" is not a name.
            return value if value and not value.startswith("_") else None
    return None

def sync_name(ident, name):
    """Settings rename: rewrite the name line of IDENTITY.md, if the file still has one."""
    try: current = read_file(ident, "IDENTITY.md")
    except (WorkspaceUnavailable, LookupError, PermissionError): return
    if current["binary"] or current["truncated"]: return
    lines, changed = current["content"].split("\n"), False
    for index, line in enumerate(lines):
        if _is_name_line(line):
            prefix = line[:line.index("**", line.index("**") + 2) + 2]
            lines[index] = prefix + " " + name
            changed = True
            break
    if changed:
        try: write_file(ident, "IDENTITY.md", "\n".join(lines), current["etag"])
        except (ValueError, PermissionError, OSError): pass
