from personal_agent.workspace.constants import DEFAULT_NAME
from personal_agent.identity.capabilities import default_capabilities
"""Owner-scoped identity operations."""
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
import uuid

from core.db.models import ChatSession

from personal_agent.models import PersonalAgent
from personal_agent.identity.config import DEFAULT_CONCURRENCY, DEFAULTS_VERSION, MAX_CONCURRENCY, MAX_NAME_LENGTH, update_config, validate_avatar

class IdentityService:
    def __init__(self, context):
        self.context = context
        self.db, self.user_id = context.db, context.user_id

    @property
    def chat_id(self):
        return self.context.chat_id

    def agent(self):
        return self.db.scalar(select(PersonalAgent).where(PersonalAgent.user_id == self.user_id))

    def ensure(self):
        row = self.agent()
        if row is None:
            # Catalog reads can seed system configuration through another DB
            # session. Finish them before taking SQLite's identity write lock.
            defaults = default_capabilities(self.db, self.user_id)
            ident = uuid.uuid4().hex
            chat_id = "pa_chat_" + ident
            self.db.add(ChatSession(chat_id=chat_id, user_id=self.user_id, title=DEFAULT_NAME, extra_data={"personal_agent": True}))
            self.db.flush()
            row = PersonalAgent(id="pa_" + ident, user_id=self.user_id, chat_id=chat_id,
                                sandbox_session_id="pa_box_" + ident, config={"name": DEFAULT_NAME, "concurrency": DEFAULT_CONCURRENCY, "email_enabled": True,
                                **{key: value or [] for key, value in defaults.items()}, "defaults_version": DEFAULTS_VERSION})
            self.db.add(row)
            try:
                self.db.commit()
            except IntegrityError:
                self.db.rollback()
                row = self.agent()
                if row is None: raise
        elif (row.config or {}).get("defaults_version", 1) < DEFAULTS_VERSION:
            # Upgrade older identities once. A later user choice is never overwritten.
            config = dict(row.config or {})
            if config.get("defaults_version", 1) < 4:
                # Connectors are on by default; email still needs a connected account and approval to send.
                config.update(email_enabled=True)
            named = "name" not in config
            if named: config["name"] = self._legacy_name(row.id)
            if "capability_selection" not in config:
                config.update({key: value for key, value in default_capabilities(self.db, self.user_id).items() if value is not None})
            config["defaults_version"] = DEFAULTS_VERSION
            row.config = config
            self.db.commit()
            if named:
                self._title(config["name"])
                from personal_agent.workspace.files import WorkspaceUnavailable
                from personal_agent.workspace.persona import sync_name
                try: sync_name(row.id, config["name"])
                except (WorkspaceUnavailable, OSError): pass
        self._sync_capabilities(row)
        return self.view(row)

    def _sync_capabilities(self, row):
        from personal_agent.identity.capabilities import available_capabilities, reconcile
        try:
            available = available_capabilities(self.db, self.user_id)
        except ValueError:
            return
        current = dict(row.config or {})
        reconcile(current, available)
        if current == (row.config or {}): return
        update_config(self.db, self.user_id, lambda config: reconcile(config, available))
        self.db.refresh(row)

    def capability_options(self):
        from personal_agent.identity.capabilities import available_capabilities
        return available_capabilities(self.db, self.user_id)

    @staticmethod
    def _legacy_name(ident):
        """An unnamed agent keeps a name it gave itself in IDENTITY.md; the old placeholder becomes Hugo."""
        from personal_agent.workspace.files import WorkspaceUnavailable
        from personal_agent.workspace.persona import identity_name
        from personal_agent.workspace.files import read_text
        try: name = identity_name(read_text(ident, "IDENTITY.md"))
        except WorkspaceUnavailable: name = None
        return name if name and name != "Personal Agent" and len(name) <= MAX_NAME_LENGTH else DEFAULT_NAME

    def view(self, row=None):
        row = row or self.agent()
        if row is None: return None
        return {"id": row.id, "chat_id": row.chat_id, "status": row.status,
                "computer_status": row.computer_status, "generation": row.generation,
                "config": {"thinking_effort": "fast", **(row.config or {})}}

    def configure(self, patch):
        self.ensure()
        allowed = {"concurrency", "email_enabled", "timezone", "name", "avatar", "heartbeat", "dreaming", "model_provider_id", "thinking_effort", "approval_mode", "skill_ids", "mcp_ids", "plugin_ids"}
        if set(patch) - allowed: raise ValueError("Unknown Personal Agent setting")
        if "approval_mode" in patch:
            from personal_agent.tasks.approvals import APPROVAL_MODES
            if patch["approval_mode"] not in APPROVAL_MODES: raise ValueError("Unknown approval mode")
        if "concurrency" in patch and (type(patch["concurrency"]) is not int or not 1 <= patch["concurrency"] <= MAX_CONCURRENCY):
            raise ValueError("Concurrency must be from 1 to " + str(MAX_CONCURRENCY))
        if "timezone" in patch:
            from personal_agent.scheduling.schedules import zone
            zone(patch["timezone"])
        if "name" in patch and (not isinstance(patch["name"], str) or not 1 <= len(patch["name"].strip()) <= MAX_NAME_LENGTH):
            raise ValueError("Name must contain 1 to " + str(MAX_NAME_LENGTH) + " characters")
        if "name" in patch: patch["name"] = patch["name"].strip()
        if "avatar" in patch: validate_avatar(patch["avatar"])
        if "thinking_effort" in patch and patch["thinking_effort"] not in {"fast", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("Invalid thinking effort")
        if "model_provider_id" in patch:
            # Empty means the platform default model. Otherwise the same allowlist as the chat model picker.
            from core.auth.model_selection import resolve_user_model_provider_id
            wanted = (patch["model_provider_id"] or "").strip()
            patch["model_provider_id"] = resolve_user_model_provider_id(self.db, wanted, user_id=self.user_id) if wanted else None
            if wanted and patch["model_provider_id"] is None: raise ValueError("Model switching is not available to this user")
        from personal_agent.identity.capabilities import SELECTION_KEYS, validate_selection, selection, reconcile
        choices = {key: patch.pop(key) for key in SELECTION_KEYS if key in patch}
        available = self.capability_options() if choices else None
        beat = patch.pop("heartbeat", None)
        dream = patch.pop("dreaming", None)

        def change(config):
            config.update(patch)
            if dream is not None:
                from personal_agent.workspace.dreaming import validate as validate_dreaming
                config["dreaming"] = validate_dreaming(dream, config)
            if choices:
                validate_selection(choices, available, selection(config))
                config["capability_selection"] = {**selection(config), **choices}
                reconcile(config, available)
            if beat is not None:
                from personal_agent.scheduling.heartbeat import validate
                # Merged under the row lock: the scheduler writes the state fields of the same object.
                config["heartbeat"] = validate(beat, config)
        update_config(self.db, self.user_id, change)
        if beat is not None and self.agent().config["heartbeat"]["enabled"]:
            from personal_agent.scheduling.heartbeat import ensure_checklist
            ensure_checklist(self.agent().id, (self.agent().config or {}).get("name"))
        if "name" in patch:
            self._title(patch["name"])
            from personal_agent.workspace.files import WorkspaceUnavailable
            from personal_agent.workspace.persona import sync_name
            try: sync_name(self.agent().id, patch["name"])
            except (WorkspaceUnavailable, OSError): pass
        return self.view(self.agent())

    def _title(self, name):
        """The agent's conversation carries its name as the title."""
        self.db.execute(update(ChatSession).where(ChatSession.chat_id == self.agent().chat_id).values(title=name))
        self.db.commit()

    def adopt_identity_name(self, content):
        """IDENTITY.md was edited by hand: its name line becomes the agent name. Settings is not rewritten back."""
        from personal_agent.workspace.persona import identity_name
        name = identity_name(content)
        if not name or len(name) > MAX_NAME_LENGTH or name == (self.agent().config or {}).get("name"): return
        update_config(self.db, self.user_id, lambda config: config.update(name=name))
        self._title(name)
