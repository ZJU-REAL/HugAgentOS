"""POSIX paths inside a leased task container, with no account aliases."""
import posixpath
import shlex


def validate_path(path, root):
    if not isinstance(path, str) or not path or "\x00" in path:
        return "Evaluation path must be a non-empty POSIX path"
    normalized = posixpath.normpath(path if path.startswith("/") else root + "/" + path)
    if any(normalized == private or normalized.startswith(private + "/")
           for private in ("/myspace", "/workspace/myspace")):
        return "Account My Space paths are unavailable in evaluation sessions"
    return None


def workspace_directory(root, session_id):
    return root


def resolve_path(path, root, session_id, user_id=None):
    return posixpath.normpath(path if path.startswith("/") else root + "/" + path)


def validate_project_scope_path(path, project_folder_name):
    return None


def is_myspace_physical(physical_path, user_id, root):
    return False


def quote_shell_path(path):
    return shlex.quote(path)


def bash_workspace_instructions(root, session_id):
    return (
        "Execute a command in this attempt's leased sandbox. All native file tools and "
        "child agents share this exact container. No account files or credentials are mounted. "
        "Use the task's requested paths; relative file paths resolve under /workspace.\n\n"
    )
