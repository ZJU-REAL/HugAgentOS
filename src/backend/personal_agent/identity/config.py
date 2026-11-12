"""Identity configuration, validation and locked updates."""

from sqlalchemy import select

from personal_agent.models import PersonalAgent

TERMINAL = {"completed", "failed", "cancelled"}

DEFAULTS_VERSION = 4
DEFAULT_CONCURRENCY = 3
MAX_CONCURRENCY = 8
MAX_NAME_LENGTH = 30

AVATAR_PRESETS = {"robot", "fox", "cat", "owl", "panda", "star", "bolt", "leaf"}

def validate_avatar(value):
    """A preset name, or a small uploaded image as a data URL. Empty restores the default."""
    import base64, re
    if value in ("", None) or (isinstance(value, str) and value.removeprefix("preset:") in AVATAR_PRESETS and value.startswith("preset:")):
        return
    match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)", value or "")
    if not match: raise ValueError("Avatar must be a preset or a PNG, JPEG or WebP image")
    if len(base64.b64decode(match.group(2), validate=True)) > 256 * 1024: raise ValueError("Avatar image exceeds 256 KiB")

def concurrency(config):
    """Tasks that may run at once for one owner."""
    return max(1, min(MAX_CONCURRENCY, int((config or {}).get("concurrency", DEFAULT_CONCURRENCY))))

def user_config(db, uid):
    return db.scalar(select(PersonalAgent.config).where(PersonalAgent.user_id == str(uid))) or {}

def locked_agent(db, uid):
    """The owner's agent row, locked: several writers share its config document."""
    row = db.scalar(select(PersonalAgent).where(PersonalAgent.user_id == str(uid)).with_for_update().execution_options(populate_existing=True))
    if row is None: raise LookupError("Personal Agent not found")
    return row

def update_config(db, uid, change):
    """Read, change and write the config document under the row lock. Returns change's result."""
    row = locked_agent(db, uid)
    config = dict(row.config or {})
    result = change(config)
    row.config = config
    db.commit()
    return result
