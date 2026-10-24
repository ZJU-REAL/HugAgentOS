"""Load skills from several sources, resolving conflicts by source priority."""
from __future__ import annotations
from core.harness.registry.local_publication import local_publication
import time
from typing import Any, Dict, List, Optional, Tuple
from core.capabilities.skills.backends import CompositeBackend, DatabaseBackend, FilesystemBackend
from core.capabilities.skills.binary_files import decode_binary, is_binary_value
from core.capabilities.skills.config import get_enabled_skill_sources, skill_files_dir
from core.capabilities.skills.registry import AgentSkillMetadata, AgentSkillSpec, _load_skill_from_file, _load_skill_from_str, _load_skill_metadata_from_file, load_skill_metadata_from_str, parse_scripts_json
import logging
from core.capabilities.skills import script_discovery

logger = logging.getLogger(__name__)

class MultiSourceSkillLoader:
    """Loads skills from multiple sources with priority-based conflict resolution.

    This loader uses the backend abstraction layer to support loading skills from:
    - Built-in skills (extensions/skills/)
    - User skills (~/.hugagent/skills/)
    - Project skills (.hugagent/skills/)

    Higher priority sources override lower priority sources for conflicting skill IDs.
    """

    def __init__(self, backend: Optional[CompositeBackend] = None):
        """Initialize the multi-source skill loader.

        Args:
            backend: Optional CompositeBackend. If not provided, creates one from
                     default skill sources.
        """
        if backend is None:
            backend = self._create_default_backend()
        self._backend = backend
        self._metadata_cache: Optional[Dict[str, AgentSkillMetadata]] = None
        self._backend_change_token: Optional[Any] = self._get_backend_change_token()
        self._last_change_check_at = time.monotonic()
        # Cache: skill_id → (base_dir_path, materialized_timestamp)
        self._materialized_cache: Dict[str, Tuple[str, float]] = {}

    @staticmethod
    def _create_default_backend() -> CompositeBackend:
        """Create a CompositeBackend from default skill sources.

        Returns:
            CompositeBackend configured with enabled skill sources.
        """
        from core.harness.registry.run_capabilities import has_run_capabilities

        sources = get_enabled_skill_sources()
        backends = []
        for src in sources:
            if src.name == "admin":
                backends.append(DatabaseBackend(priority=src.priority))
            elif src.name == "cloud":

                backends.append(local_publication().skill_backend(priority=src.priority))
            else:
                backends.append(
                    FilesystemBackend(
                        root_dir=src.root_dir,
                        source_name=src.name,
                        priority=src.priority,
                    )
                )
        if not has_run_capabilities():
            return CompositeBackend(backends)

        backends.append(local_publication().skill_backend(local=True))

        # Desktop store: same-id clashes go to the resolver, never to priority.

        return CompositeBackend(
            backends,
            merge=lambda groups, backend_hash: local_publication().merge_skill_infos(groups, backend_hash=backend_hash),
        )

    def _get_backend_change_token(self) -> Optional[Any]:
        """Return a backend change token when the backend supports it."""
        token_fn = getattr(self._backend, "change_token", None)
        if not callable(token_fn):
            return None
        return token_fn()

    def _sync_backend_cache(self, *, force: bool = False) -> None:
        """Refresh cached backend state if DB-backed skills changed externally."""
        now = time.monotonic()
        if not force and now - self._last_change_check_at < 1.0:
            return

        current_token = self._get_backend_change_token()
        self._last_change_check_at = now
        if current_token == self._backend_change_token:
            return

        logger.info("Skill source changed; refreshing skill loader cache")
        self._metadata_cache = None
        self._materialized_cache.clear()
        self._backend_change_token = current_token

    def load_all_metadata(self) -> Dict[str, AgentSkillMetadata]:
        """Load metadata for all skills (fast, no instructions parsing).

        Uses a metadata cache, but invalidates it when DB-managed skills change
        in another process (for example the skill-manager MCP container).

        Returns:
            Dictionary mapping skill_id to AgentSkillMetadata.
        """
        self._sync_backend_cache(force=True)
        if self._metadata_cache is not None:
            return self._metadata_cache

        metadata_map: Dict[str, AgentSkillMetadata] = {}
        skill_files = self._backend.list_skill_files()

        for skill_info in skill_files:
            try:
                if skill_info.metadata is not None:
                    item = skill_info.metadata
                    metadata = AgentSkillMetadata(
                        id=str(item.get("id") or skill_info.skill_id),
                        name=str(item.get("name") or skill_info.skill_id),
                        description=str(item.get("description") or ""),
                        version=str(item.get("version") or "1.0.0"),
                        tags=list(item.get("tags") or []),
                        allowed_tools=list(item.get("allowed_tools") or []),
                        mcp_server_ids=list(item.get("mcp_server_ids") or []),
                    )
                elif skill_info.content is not None:
                    metadata = load_skill_metadata_from_str(
                        skill_info.content, skill_info.skill_id
                    )
                else:
                    metadata = _load_skill_metadata_from_file(skill_info.file_path)
                # Add source information to skill_path for debugging
                metadata_with_source = AgentSkillMetadata(
                    id=script_discovery.bound_skill_id(skill_info, metadata.id),
                    name=metadata.name,
                    description=metadata.description,
                    version=metadata.version,
                    tags=metadata.tags,
                    allowed_tools=metadata.allowed_tools,
                    mcp_server_ids=metadata.mcp_server_ids,
                    skill_path=f"{skill_info.source_name}:{skill_info.file_path}",
                )
                metadata_map[metadata_with_source.id] = metadata_with_source
            except Exception as e:
                # Log warning but continue loading other skills
                logger.warning("Failed to load skill metadata from %s: %s", skill_info.file_path, e)
                continue

        self._metadata_cache = metadata_map
        return metadata_map

    def load_skill_full(self, skill_id: str) -> Optional[AgentSkillSpec]:
        """Load full spec for a single skill by id (on-demand loading).

        Args:
            skill_id: The skill identifier.

        Returns:
            AgentSkillSpec if found, None otherwise.
        """
        # First check if skill exists in metadata
        metadata_map = self.load_all_metadata()
        if skill_id not in metadata_map:
            return None

        # Get the skill file info to find the path
        skill_info = self._backend.get_skill_info(skill_id)
        if skill_info is None:
            return None

        try:
            if skill_info.is_database:
                spec = _load_skill_from_str(
                    self._backend.read_skill_file(skill_id),
                    skill_info.skill_id,
                )
            elif skill_info.content is not None:
                spec = _load_skill_from_str(skill_info.content, skill_info.skill_id)
            else:
                spec = _load_skill_from_file(skill_info.file_path)
            # Populate extra_files list from backend
            extra_file_names: List[str] = []
            ef: Dict[str, str] = {}
            try:
                ef = self._backend.get_extra_files(skill_id)
                extra_file_names = sorted(ef.keys())
            except Exception as fallback_error:
                from core.infra.failures import record_best_effort_failure
                record_best_effort_failure('core.capabilities.skills.loader:load_skill_full', fallback_error)

            # Resolve base_dir for {baseDir} substitution
            base_dir = ""
            if not skill_info.is_database and skill_info.content is None:
                # Filesystem skill: folder containing SKILL.md
                base_dir = str(skill_info.file_path.parent)
            elif extra_file_names:
                # DB skill with extra files: materialize to cache dir
                # Pass the already-fetched ef dict to avoid a second DB query
                base_dir = self._materialize_skill_files(skill_id, extra_files=ef)

            # Parse _scripts.json if present
            executable_scripts: List[Dict] = []
            scripts_json_content = ef.get("_scripts.json", "")
            if (
                not scripts_json_content
                and not skill_info.is_database
                and skill_info.content is None
            ):
                # Filesystem skill: check file directly
                scripts_path = skill_info.file_path.parent / "_scripts.json"
                if scripts_path.is_file():
                    scripts_json_content = scripts_path.read_text(encoding="utf-8")
            if scripts_json_content:
                try:
                    executable_scripts = parse_scripts_json(scripts_json_content)
                except Exception as e:
                    logger.warning("Failed to parse _scripts.json for skill '%s': %s", skill_id, e)

            # Auto-generate whitelist from .py files when _scripts.json is absent
            if not executable_scripts:
                auto_scripts = script_discovery.auto_detect_scripts(ef, skill_info)
                if auto_scripts:
                    executable_scripts = auto_scripts
                    logger.info(
                        "Auto-detected %d script(s) for skill '%s': %s",
                        len(auto_scripts),
                        skill_id,
                        [s["name"] for s in auto_scripts],
                    )

            # Add source information to skill_path
            spec_with_source = AgentSkillSpec(
                id=script_discovery.bound_skill_id(skill_info, spec.id),
                name=spec.name,
                description=spec.description,
                version=spec.version,
                instructions=spec.instructions,
                inputs=spec.inputs,
                outputs=spec.outputs,
                tags=spec.tags,
                allowed_tools=spec.allowed_tools,
                mcp_server_ids=spec.mcp_server_ids,
                extra_files=extra_file_names,
                base_dir=base_dir,
                examples=spec.examples,
                executable_scripts=executable_scripts,
                skill_path=f"{skill_info.source_name}:{skill_info.file_path}",
            )
            return spec_with_source
        except Exception as e:
            logger.warning("Failed to load skill spec for %s: %s", skill_id, e)
            return None

    def clear_cache(self):
        """Clear the metadata cache (useful for testing or hot-reloading)."""
        refresh_fn = getattr(self._backend, "refresh", None)
        if callable(refresh_fn):
            refresh_fn()
        self._metadata_cache = None
        self._backend_change_token = self._get_backend_change_token()
        self._last_change_check_at = time.monotonic()
        self._materialized_cache.clear()

    def get_extra_files(self, skill_id: str) -> Dict[str, str]:
        """Get extra files for a skill.

        Returns:
            {filename: content} dict.
        """
        self._sync_backend_cache()
        return self._backend.get_extra_files(skill_id)

    def get_skill_base_dir(self, skill_id: str) -> Optional[str]:
        """Get the base directory for a skill.

        For filesystem skills: returns the actual skill folder path.
        For DB skills: materializes extra_files to a persistent cache directory
        and returns that path.

        Returns:
            Absolute path to the skill's working directory, or None.
        """
        skill_info = self._backend.get_skill_info(skill_id)
        if skill_info is None:
            return None

        # Filesystem skills: the folder containing SKILL.md
        if not skill_info.is_database and skill_info.content is None:
            return str(skill_info.file_path.parent)

        # DB skills: materialize to a persistent cache dir
        return self._materialize_skill_files(skill_id)

    def get_skill_owner(self, skill_id: str) -> Optional[str]:
        """The user a private skill belongs to (None for built-in / global skills).

        Callers that hand skill files to a sandbox use this to refuse someone
        else's private skill — the loader itself is global and resolves every id.
        """
        try:
            info = self._backend.get_skill_info(skill_id)
        except Exception:  # noqa: BLE001
            return None
        if info is None or not info.metadata:
            return None
        owner = info.metadata.get("owner_user_id")
        return str(owner) if owner else None

    def _materialize_skill_files(
        self, skill_id: str, extra_files: Optional[Dict[str, str]] = None
    ) -> str:
        """Write DB extra_files to disk so scripts can be executed.

        Cloud publication reads a current source snapshot under the shared
        publication lock. The complete tree replaces the previous version;
        unchanged trees are verified and reused.

        Args:
            skill_id: The skill identifier.
            extra_files: Pre-fetched {filename: content} dict. If None,
                         fetches from backend (saves a DB round-trip when
                         the caller already has the data).

        Returns:
            Absolute path to the materialized directory.
        """
        from core.harness.registry.run_capabilities import has_run_capabilities
        from core.capabilities.skills.publication import decode_files, publication_lock, publish_tree

        if has_run_capabilities():
            # The desktop capability store already publishes immutable revisions.
            content, current_files, owner = self._backend.read_snapshot(skill_id)
            return self._publish_to_store(skill_id, content, current_files, owner)

        # Never publish caller-prefetched extra_files or a cached owner. An old
        # loader may outlive an update or deletion committed by another worker.
        with publication_lock():
            content, current_files, owner = self._backend.read_snapshot(skill_id)
            target = skill_files_dir(skill_id, owner)
            publish_tree(target, decode_files(content, current_files))
            return str(target)

    def _publish_to_store(
        self,
        skill_id: str,
        content: str,
        extra_files: Dict[str, str],
        owner: Optional[str],
    ) -> str:
        """Desktop store path of a DB skill: publish its current content, link the views."""
        from core.capabilities.manifest_protocol import skill_content_hash

        info = self._backend.get_skill_info(skill_id)
        meta = dict(info.metadata or {}) if info else {}
        files: Dict[str, bytes | str] = {"SKILL.md": content}
        for filename, body in extra_files.items():
            files[filename] = decode_binary(body) if is_binary_value(body) else str(body)
        before_views = local_publication().view_generation()
        comp = local_publication().publish_local_skill(
            skill_id,
            files=files,
            content_hash=skill_content_hash(content, extra_files),
            owner_user_id=owner,
            source="plugin" if meta.get("source_plugin") else "local",
            source_plugin=meta.get("source_plugin"),
            display_name=str(meta.get("name") or skill_id),
            description=str(meta.get("description") or ""),
            version=str(meta.get("version") or ""),
            from_db=True,
        )
        # A view is a function of the resolved set, and the publication bumps the
        # view generation exactly when that set moved. Re-materializing unchanged
        # content leaves every existing link pointing at the right revision, so
        # relinking every name again would only re-derive the same answer.
        if local_publication().view_generation() != before_views:
            local_publication().rebuild_views(owner)
            logger.info("Published skill '%s' to store revision %s", skill_id, comp.revision)
        return str(comp.path)

    def get_skill_dir(self, skill_id: str) -> Optional[str]:
        """Get the on-disk directory for a skill.

        For filesystem skills: returns the actual skill folder.
        For DB skills: materializes extra_files to cache and returns that dir.
        This is the path that can be passed to AgentScope's
        ``toolkit.register_agent_skill()``.

        Returns:
            Absolute path to a directory containing SKILL.md, or None.
        """
        skill_info = self._backend.get_skill_info(skill_id)
        if skill_info is None:
            return None

        # Filesystem skill: SKILL.md parent dir
        if not skill_info.is_database and skill_info.content is None:
            return str(skill_info.file_path.parent)

        # DB skill: materialize to cache dir so AgentScope can read it
        ef = self._backend.get_extra_files(skill_id)
        return self._materialize_skill_files(skill_id, extra_files=ef)

    def register_skills_to_toolkit(
        self,
        toolkit,
        skill_ids: Optional[List[str]] = None,
    ) -> int:
        """Register skills into an AgentScope Toolkit using its native
        ``register_agent_skill(skill_dir)`` API.

        For each skill:
        - Filesystem skills: pass the existing directory directly.
        - DB skills: materialize to the unified sandbox skills dir
          (``get_sandbox_skills_dir()/<id>/``) first, then register that dir.

        Args:
            toolkit: An ``agentscope.tool.Toolkit`` instance.
            skill_ids: Optional whitelist. If None, registers all skills.

        Returns:
            Number of skills successfully registered.
        """
        metadata = self.load_all_metadata()
        ids_to_register = skill_ids if skill_ids is not None else list(metadata.keys())

        count = 0
        for sid in ids_to_register:
            if sid not in metadata:
                continue
            try:
                skill_dir = self.get_skill_dir(sid)
                if skill_dir is None:
                    continue
                from core.capabilities.skills.runtime_loader import RuntimeNamedSkillLoader
                toolkit.skill_loaders.append(RuntimeNamedSkillLoader(
                    skill_dir, sid, getattr(self, "capability_run", None)
                ))
                count += 1
            except Exception as e:
                logger.warning("Failed to register skill '%s': %s", sid, e)
        return count

    def get_skill_source(self, skill_id: str) -> Optional[str]:
        """Get the source name for a skill (useful for debugging).

        Args:
            skill_id: The skill identifier.

        Returns:
            Source name (e.g., "built-in", "user", "project") or None if not found.
        """
        self._sync_backend_cache()
        skill_info = self._backend.get_skill_info(skill_id)
        return skill_info.source_name if skill_info else None
