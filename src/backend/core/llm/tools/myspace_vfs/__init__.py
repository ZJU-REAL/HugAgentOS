# Stable public facade for path resolution, sandbox reads and reverse sync.
from core.llm.tools.myspace_vfs.io import (
    _is_text_name,
    _resolve_root,
    glob_tree,
    iter_tree,
    materialize_into_sandbox,
    materialize_tree,
)
from core.llm.tools.myspace_vfs.mutations import (
    _remove_cache,
    sync_delete,
    sync_mkdir,
    sync_move,
    sync_upsert,
)
from core.llm.tools.myspace_vfs.paths import (
    MYSPACE_LOGICAL,
    WORKSPACE_ROOT,
    FolderResolve,
    _apply_scope_to_rel,
    _guess_mime,
    mirror_to_cache,
    myspace_cache_file,
    myspace_rel,
    resolve_artifact,
    resolve_file_id,
    resolve_folder_id,
    settings,
    split_rel,
)
