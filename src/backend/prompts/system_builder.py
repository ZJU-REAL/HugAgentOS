"""System prompt assembly; cache and public hooks live in prompt_runtime."""
from __future__ import annotations
from prompts import prompt_runtime as runtime

def build_system_prompt(
    config: runtime.PromptConfig,
    ctx: runtime.Dict[str, runtime.Any] | None = None,
    *,
    manifest_builder: runtime.Optional[runtime.PromptManifestBuilder] = None,
    desktop_preview: bool = False,
) -> str:
    """Build the system prompt from config + runtime context.

    Results are cached with a 300s TTL. The {now} placeholder is replaced
    at render time so the cache isn't invalidated every second.

    Adds a *dynamic* appendix describing currently-available tools (name + short description)
    so tools remain pluggable without hardcoding tool names in the static prompt.

    Fallback order:
      1) Filesystem prompt (config.system_prompt.prompt_dir / env PROMPT_DIR)
      2) Inline template (config.system_prompt.inline_template or env PROMPT_INLINE_TEMPLATE)
      3) Minimal hardcoded fallback (guarantee non-empty)

    Args:
        config: Loaded PromptConfig.
        ctx: Runtime context (optional). Recognized keys:
            - now: override timestamp
            - tools: optional iterable of tool objects (each with `.name` and `.description`)
            - mcp_servers: list of enabled MCP server keys
    Returns:
        A non-empty system prompt string.
    """

    ctx = dict(ctx or {})
    from core.config.local_mode import local_mode_enabled

    is_local = desktop_preview or local_mode_enabled()
    # Snapshot before hashing: permission/path changes must invalidate cached facts.
    if is_local:
        from prompts.desktop_templates import desktop_parts
        ctx["desktop_parts"] = desktop_parts()
        ctx["desktop_environment"] = (ctx.get("preview_environment") if desktop_preview else None) or runtime.build_environment_context(
            ctx, current_date=runtime._PROMPT_NOW_SENTINEL
        )
    # Day granularity only: the date is constant within a day → the system prompt is
    # byte-stable all day → LLM prefix cache hits all day (a second-level timestamp
    # would change every request and bust the cache).
    now = ctx.get("now") or runtime.datetime.now().strftime("%Y-%m-%d")

    # Build cache key from stable inputs (excluding {now})
    prompt_dir_cfg = (
        getattr(config.system_prompt, "prompt_dir", None) or "./prompts/prompt_text/default"
    )
    parts_key = tuple(config.system_prompt.parts) if config.system_prompt.parts else ()
    tool_names = runtime._extract_tool_names(ctx.get("tools"))
    mcp_keys = tuple(sorted(ctx.get("mcp_servers") or []))
    provider_key = (getattr(config.system_prompt, "provider", None) or "filesystem").strip().lower()
    enabled_kbs_key = tuple(sorted(ctx.get("enabled_kbs") or []))
    # Full canonical hash, not plaintext/truncated signatures. This covers the
    # complete project instructions, complete files manifest, tool definitions
    # and any future dynamic template variable without expanding the cache key
    # into a sensitive in-memory tuple.
    prompt_context_hash = runtime._prompt_cache_context_hash(ctx)

    # Active pool version (preferred source) — bust cache on change
    active_version_key: tuple[str, str] = ("", "")
    try:
        from core.services import prompt_version_service as pvs

        _av = pvs.get_active_version("system")
        if _av:
            active_version_key = (str(_av.get("id") or ""), str(_av.get("updated_at") or ""))
    except Exception:
        pass

    # Include DB prompt parts version in cache key for invalidation
    db_version = runtime._get_db_prompt_version()
    cache_key = (
        provider_key,
        str(prompt_dir_cfg),
        parts_key,
        tool_names,
        mcp_keys,
        db_version,
        enabled_kbs_key,
        active_version_key,
        prompt_context_hash,
    )

    # Check cache
    with runtime._prompt_cache_lock:
        cached = runtime._prompt_cache.get(cache_key)
        if cached is not None:
            expires_at, template, cached_sections = cached
            if runtime.monotonic() < expires_at:
                if manifest_builder is not None:
                    for spec in cached_sections:
                        manifest_builder.add_prompt_section(
                            spec["id"],
                            str(spec["content_template"]).replace(runtime._PROMPT_NOW_SENTINEL, now),
                            origin=spec["origin"],
                            trust=spec["trust"],
                            priority=spec["priority"],
                            cache_class=spec["cache_class"],
                            budget=spec["budget"],
                            version=spec["version"],
                            reference=spec.get("reference"),
                            sensitive=spec.get("sensitive", False),
                        )
                return template.replace(runtime._PROMPT_NOW_SENTINEL, now)
            else:
                runtime._prompt_cache.pop(cache_key, None)

    # Cache miss — build the prompt
    strict_vars = runtime._env_bool("PROMPT_STRICT_VARS", True)

    section_specs: runtime.List[runtime.Dict[str, runtime.Any]] = []

    def _record_section(
        section_id: str,
        content: str,
        *,
        origin: str,
        trust: str,
        priority: int,
        cache_class: str,
        version: str = "1",
        reference: runtime.Optional[str] = None,
        sensitive: bool = False,
        budget: runtime.Optional[int] = None,
    ) -> None:
        if not str(content or "").strip():
            return
        estimated_budget = max(1, (len(str(content).encode("utf-8")) + 3) // 4)
        section_specs.append(
            {
                "id": str(section_id),
                "content_template": str(content),
                "origin": str(origin),
                "trust": str(trust),
                "priority": int(priority),
                "cache_class": str(cache_class),
                "budget": int(budget if budget is not None else estimated_budget),
                "version": str(version or "1"),
                "reference": reference,
                "sensitive": bool(sensitive),
            }
        )

    provider = provider_key
    base = ""

    # ── Try DB-backed prompt parts first ──────────────────────────────
    db_parts = runtime._load_db_prompt_parts()

    # ── Preferred source: active version in prompt_version_service ────
    # When a version is active in ContentBlock(id=prompt_versions), its parts
    # are the source of truth. AdminPromptPart overlay still wins on part_id
    # collision for backward compatibility with rows created by the old admin UI.
    active_system_version: runtime.Optional[runtime.Dict[str, runtime.Any]] = None
    try:
        from core.services import prompt_version_service as pvs

        active_system_version = pvs.get_active_version("system")
    except Exception:
        active_system_version = None

    if active_system_version and active_system_version.get("parts"):
        from prompts.provider import render_template

        chunks: runtime.List[str] = []
        seen_ids: set[str] = set()
        for p in active_system_version["parts"]:
            pid = (p.get("part_id") or "").strip()
            if not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)
            # project_mode is a dynamic appendix section (injected only for project chats); never enters the base prompt
            if pid == runtime.PROJECT_MODE_PART_ID:
                continue
            # DB override wins if present
            db_row = db_parts.get(pid)
            if db_row:
                if not db_row.get("is_enabled", True):
                    continue
                content = db_row["content"]
            else:
                if not p.get("is_enabled", True):
                    continue
                content = p.get("content") or ""
            txt = render_template(content, vars={**ctx, "now": runtime._PROMPT_NOW_SENTINEL}, strict=False)
            if is_local:
                txt = runtime.desktop_prompt_text(txt)
            if txt.strip():
                chunks.append(txt.strip())
                _record_section(
                    pid,
                    txt.strip(),
                    origin=(
                        "database:admin_prompt_parts"
                        if db_row
                        else f"prompt-version:{active_system_version.get('id') or 'system'}"
                    ),
                    trust="admin",
                    priority=int(p.get("sort_order") or len(chunks) * 10),
                    cache_class="stable_prefix",
                    version=(db_version if db_row else str(active_system_version.get("id") or "1")),
                )
        # Include any DB-only parts not listed in the active version
        for pid, db_row in db_parts.items():
            if pid in seen_ids or not db_row.get("is_enabled", True):
                continue
            if pid == runtime.PROJECT_MODE_PART_ID:
                continue  # same as above: the dynamic appendix section never enters base
            txt = render_template(
                db_row["content"], vars={**ctx, "now": runtime._PROMPT_NOW_SENTINEL}, strict=False
            )
            if is_local:
                txt = runtime.desktop_prompt_text(txt)
            if txt.strip():
                chunks.append(txt.strip())
                _record_section(
                    pid,
                    txt.strip(),
                    origin="database:admin_prompt_parts",
                    trust="admin",
                    priority=int(db_row.get("sort_order") or len(chunks) * 10),
                    cache_class="stable_prefix",
                    version=db_version or "1",
                )
        base = "\n\n".join(chunks).strip()

    # 1) Filesystem prompt: config-driven prompt pack.
    if (not base.strip()) and provider == "filesystem":
        prompt_dir = runtime._resolve_prompt_dir(str(prompt_dir_cfg))
        fs_provider = runtime.FilesystemPromptProvider(prompt_dir=prompt_dir, strict_vars=strict_vars)

        parts = getattr(config.system_prompt, "parts", None)
        if isinstance(parts, list) and parts:
            # Build merged parts list: filesystem + DB-only parts
            all_part_ids = list(parts)
            for pid in db_parts:
                if pid not in all_part_ids:
                    all_part_ids.append(pid)

            # Sort by DB sort_order if available, else filesystem index * 10
            def _sort_key(pid: str) -> int:
                if pid in db_parts:
                    return db_parts[pid]["sort_order"]
                try:
                    return parts.index(pid) * 10
                except ValueError:
                    return 9999

            sorted_ids = sorted(all_part_ids, key=_sort_key) if db_parts else parts

            chunks: runtime.List[str] = []
            for part_id in sorted_ids:
                part_id_str = part_id.strip() if isinstance(part_id, str) else ""
                if not part_id_str:
                    continue
                # Dynamic appendix section: only injected for project chats by _build_project_section; never enters base
                if part_id_str == runtime.PROJECT_MODE_PART_ID:
                    continue

                db_row = db_parts.get(part_id_str)
                if db_row:
                    # DB override: check is_enabled
                    if not db_row["is_enabled"]:
                        continue
                    txt = db_row["content"]
                    # Apply variable substitution
                    from prompts.provider import render_template

                    txt = render_template(
                        txt, vars={**ctx, "now": runtime._PROMPT_NOW_SENTINEL}, strict=False
                    )
                else:
                    txt = fs_provider.get_prompt(
                        part_id_str, "system", vars={**ctx, "now": runtime._PROMPT_NOW_SENTINEL}
                    )

                if is_local:
                    txt = runtime.desktop_prompt_text(txt)
                if txt.strip():
                    chunks.append(txt.strip())
                    _record_section(
                        part_id_str,
                        txt.strip(),
                        origin=(
                            "database:admin_prompt_parts" if db_row else f"filesystem:{prompt_dir}"
                        ),
                        trust="admin" if db_row else "platform",
                        priority=int((db_row or {}).get("sort_order") or _sort_key(part_id_str)),
                        cache_class="stable_prefix",
                        version=db_version if db_row else str(config.version),
                    )
            base = "\n\n".join(chunks).strip()
        else:
            # Backward compatible single-file convention: system.system.md
            base = fs_provider.get_prompt(
                "system", "system", vars={**ctx, "now": runtime._PROMPT_NOW_SENTINEL}
            )
            if is_local:
                base = runtime.desktop_prompt_text(base)
            _record_section(
                "system",
                base,
                origin=f"filesystem:{prompt_dir}",
                trust="platform",
                priority=10,
                cache_class="stable_prefix",
                version=str(config.version),
            )

    # 2) Inline prompt.
    if (not base.strip()) and provider == "inline":
        inline_provider = runtime.InlinePromptProvider(
            template=(
                getattr(config.system_prompt, "inline_template", "")
                or runtime.os.getenv("PROMPT_INLINE_TEMPLATE", "")
            ),
            strict_vars=strict_vars,
        )
        base = inline_provider.get_prompt(
            "system", "system", vars={**ctx, "now": runtime._PROMPT_NOW_SENTINEL}
        )
        if is_local:
            base = runtime.desktop_prompt_text(base)
        _record_section(
            "system/inline",
            base,
            origin="config:inline",
            trust="admin",
            priority=10,
            cache_class="stable_prefix",
            version=str(config.version),
        )

    # 3) Absolute minimal fallback (guarantee non-empty).
    if not base.strip():
        base = runtime.hardcoded_minimal_system_prompt().strip()
        _record_section(
            "system/fallback",
            base,
            origin="builtin:fallback",
            trust="platform",
            priority=10,
            cache_class="stable_prefix",
            version="1",
        )

    tools = ctx.get("tools")

    if tools:
        base = (base + "\n\n" + runtime._TOOLS_AND_SKILLS_NOTICE).strip()
        _record_section(
            "runtime/tools_notice",
            runtime._TOOLS_AND_SKILLS_NOTICE,
            origin="builtin:agent_factory",
            trust="platform",
            priority=700,
            cache_class="toolset",
            version="1",
        )

    # ── Lightweight KB catalog (name + description only) ──
    enabled_kbs = ctx.get("enabled_kbs")
    if enabled_kbs:
        kb_section = runtime._build_kb_lite_section(enabled_kbs)
        if kb_section:
            base = (base + "\n\n" + kb_section).strip()
            _record_section(
                "runtime/kb_catalog",
                kb_section,
                origin="knowledge-base:catalog",
                trust="configured_service",
                priority=800,
                cache_class="capability_set",
                version="1",
                sensitive=True,
            )

    from core.services.edition_workspace import workspace_instructions
    workspace_hint = workspace_instructions()
    if workspace_hint:
        base = (base + "\n\n" + workspace_hint).strip()
        _record_section("runtime/workspace", workspace_hint, origin="runtime", trust="system",
                        priority=900, cache_class="static")

    # ── Project mode (when mounted in a Claude-style workspace) ──
    project_id = ctx.get("project_id")
    if project_id:
        if ctx.get("project_is_local"):
            # Desktop local project: real host folder, not a MySpace view (#05).
            from prompts.project_section import _build_local_project_section

            proj_section = _build_local_project_section(
                project_name=ctx.get("project_name") or "",
                project_instructions=ctx.get("project_instructions") or "",
                local_path=ctx.get("project_local_path") or "",
                local_slug=ctx.get("project_local_slug") or "",
            )
        else:
            from core.services.edition_workspace import context_directory
            proj_section = runtime._build_project_section(
                workspace_path=context_directory(ctx),
                project_name=ctx.get("project_name") or "",
                project_instructions=ctx.get("project_instructions") or "",
                folder_name=ctx.get("project_folder_name") or "",
                folder_kind=ctx.get("project_folder_kind") or "",
                project_files=ctx.get("project_files") or [],
                project_file_count=ctx.get("project_file_count"),
            )
        if proj_section:
            base = (base + "\n\n" + proj_section).strip()
            _record_section(
                "runtime/project",
                proj_section,
                origin=f"workspace:{project_id}",
                trust="workspace",
                priority=900,
                cache_class="workspace",
                version=prompt_context_hash,
                reference=f"project:{project_id}",
                sensitive=True,
            )

    # Desktop facts are independent of project instructions and remain in the
    # system prompt when agent_factory extracts runtime/project as a ContextItem.
    if is_local:
        environment = ctx["desktop_environment"]
        guidance = runtime.build_local_mode_guidance()
        for section_id, content in (
            ("runtime/environment", environment),
            ("runtime/local_mode", guidance),
        ):
            base = (base + "\n\n" + content).strip()
            _record_section(
                section_id, content, origin="builtin:local_mode", trust="platform",
                priority=950, cache_class="workspace", version=prompt_context_hash,
                sensitive=True,
            )

    # Store template in cache (with placeholder instead of real time)
    # Only the renderer-owned sentinel is dynamic. Replacing every occurrence
    # of today's date would corrupt project instructions that intentionally
    # contain that literal date when the cache is reused tomorrow.
    template = base

    cached_section_specs = tuple(
        {
            **spec,
            "content_template": str(spec["content_template"]),
        }
        for spec in section_specs
    )
    with runtime._prompt_cache_lock:
        runtime._prompt_cache[cache_key] = (
            runtime.monotonic() + runtime._PROMPT_CACHE_TTL,
            template,
            cached_section_specs,
        )

    if manifest_builder is not None:
        for spec in cached_section_specs:
            manifest_builder.add_prompt_section(
                spec["id"],
                str(spec["content_template"]).replace(runtime._PROMPT_NOW_SENTINEL, now),
                origin=spec["origin"],
                trust=spec["trust"],
                priority=spec["priority"],
                cache_class=spec["cache_class"],
                budget=spec["budget"],
                version=spec["version"],
                reference=spec.get("reference"),
                sensitive=spec.get("sensitive", False),
            )

    # Return with real time
    return template.replace(runtime._PROMPT_NOW_SENTINEL, now)
