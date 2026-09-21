"""Desktop environment facts; container prompts keep their own path semantics."""

from datetime import datetime
import platform
from xml.etree.ElementTree import Element, SubElement, indent, tostring


def build_environment_context(ctx: dict, *, current_date: str) -> str:
    """Describe the actual runner cwd separately from the selected project."""
    from core.sandbox._common import WORKSPACE
    from core.sandbox.desktop_paths import workspace_directory
    from core.llm.tool_permissions import normalize_approval_mode
    from core.services.local_grant_service import grants_for_gate, policy_for_gate

    session = str(ctx.get("sandbox_session_id") or ctx.get("chat_id") or "").strip()
    cwd = workspace_directory(WORKSPACE, session)
    root = Element("environment_context")
    SubElement(root, "cwd").text = cwd
    SubElement(root, "os").text = platform.system()
    # The public bash tool invokes Bash on all platforms, including bundled Git Bash on Windows.
    from services.script_runner_service.runtime_tools import resolve_bash_executable
    import os
    executable = resolve_bash_executable()
    SubElement(root, "shell").text = os.path.splitext(os.path.basename(executable))[0] if executable else "unavailable"
    SubElement(root, "shell_executable").text = executable or "unavailable"
    SubElement(root, "architecture").text = platform.machine()
    SubElement(root, "os_release").text = platform.release()
    SubElement(root, "current_date").text = current_date
    local_time = datetime.now().astimezone()
    SubElement(root, "timezone").text = local_time.strftime("%Z (UTC%z)")
    if ctx.get("project_is_local") and ctx.get("project_local_path"):
        SubElement(root, "project_root").text = str(ctx["project_local_path"])
    fs = SubElement(root, "filesystem")
    roots = SubElement(fs, "workspace_roots")
    SubElement(roots, "root").text = cwd
    mode = normalize_approval_mode(ctx.get("approval_mode"))
    try:
        grants = grants_for_gate()
        policy = policy_for_gate(mode)
    except Exception:
        # Facts unavailable: do not invent permissions. The execution gate remains authoritative.
        SubElement(fs, "permission_profile", type="unavailable")
    else:
        granted = SubElement(fs, "granted_roots")
        for grant in grants:
            SubElement(granted, "root", mode=grant.mode).text = grant.path
        profile = SubElement(fs, "permission_profile", type=mode)
        SubElement(profile, "workspace_write").text = policy.workspace_write
        SubElement(profile, "out_of_scope").text = policy.out_of_scope
    indent(root, space="  ")
    from prompts.desktop_templates import render_desktop_part
    return render_desktop_part("environment", environment_xml=tostring(root, encoding="unicode", short_empty_elements=False))


def desktop_prompt_text(text: str) -> str:
    """Desktop owns file-delivery rules; omit the legacy cloud output section.

    Select by its Markdown section boundary, not tool-name substitutions. Other
    sections (including administrator role/citation instructions) stay intact.
    Both filesystem templates and database prompt versions use this heading.
    """
    import re

    return re.sub(
        r"(?m)^### 输出约束（强制）[ \t]*\r?\n.*?(?=^#{1,3} |\Z)",
        "", text, flags=re.DOTALL,
    ).strip()


def build_local_mode_guidance() -> str:
    from prompts.desktop_templates import render_desktop_part
    parts = [render_desktop_part("guidance")]
    if platform.system() == "Windows":
        parts.append(render_desktop_part("windows"))
    return "\n".join(p for p in parts if p)


SKIP_PARTS = {
    "code_exec": {"system/00_sandbox_environment", "system/10_tools_and_capabilities"},
}
