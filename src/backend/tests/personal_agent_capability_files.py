"""Fixture workspaces with synced skills and plugins, as the capability sync leaves them."""
import json


def workspace(monkeypatch, root, ident="pa_x"):
    """The agent's workspace directory under a local volume root."""
    monkeypatch.setenv("PERSONAL_AGENT_LOCAL_VOLUME_ROOT", str(root))
    path = root / ident / "workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


def skill(ws, rel, description):
    """A skill directory at ws/rel with a SKILL.md carrying description."""
    base = ws / rel
    base.mkdir(parents=True, exist_ok=True)
    (base / "SKILL.md").write_text("---\nname: " + base.name + "\ndescription: " + description + "\n---\n\n# " + base.name + "\n")
    return base


def manifest(ws, kind, entries):
    """/workspace/<kind>/.synced.json for kind "skills" or "plugins"."""
    (ws / kind).mkdir(parents=True, exist_ok=True)
    (ws / kind / ".synced.json").write_text(json.dumps({"version": 1, kind: entries}))
