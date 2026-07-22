# Projects & MySpace

> Last updated: 2026-06-11

HugAgentOS provides two tiers of personal workspace:

- **MySpace**: the user's file asset hub — uploads, AI conversation outputs, a personal folder tree, chat favorites, share records, and notifications;
- **Projects**: workspaces — a set of files (a linked folder) + project instructions + an isolated memory scope, automatically carried into every conversation started inside the project. Projects come in two kinds, `personal` and `team` (team projects depend on the teams system, Enterprise Edition, EE).

The two are connected through **strong folder linking**: a project is not a separate file container — it hangs directly off a folder in MySpace (or team space), so project file operations are simply artifact operations under that folder.

## Data model

```
users_shadow ──┬── user_folders (personal folder tree, NULL parent = root)
               ├── artifacts (file assets; user_folder_id locates the personal folder)
               └── projects (kind=personal, linked_folder_id → user_folders)

teams ─────────┬── team_members (role: owner/admin/member + file_permission: viewer/editor)
(EE)           ├── team_folders (team folder tree)
               ├── artifacts (non-NULL team_id + team_folder_id = team file)
               └── projects (kind=team, linked_team_folder_id → team_folders)
```

Key ORM models (`src/backend/core/db/models/`):

| Model | Table | Highlights |
|---|---|---|
| `Project` | `projects` | `kind` (personal/team), `instructions`, mutually exclusive `linked_folder_id` / `linked_team_folder_id`, `pinned`, `metadata` (incl. project-level memory toggles); CHECK constraints keep kind and team_id consistent |
| `ProjectFavorite` | `project_favorites` | Per-user stars that don't affect anyone else's view |
| `UserFolder` | `user_folders` | Personal folder tree; name-safety constraints (no `/`, `.`, `..`) |
| `Artifact` | `artifacts` | The file itself: `storage_key` (object storage), mutually exclusive `user_folder_id` / `team_folder_id`, `parsed_text` / `summary` cross-turn read caches, soft delete via `deleted_at` |
| `Team` / `TeamMember` | `teams` / `team_members` | Teams and members (Enterprise Edition, EE); can be auto-created from external SSO departments (`source=sso_auto`) |
| `TeamFolder` | `team_folders` | Team folder tree (Enterprise Edition, EE) |

## Projects

Routes: `src/backend/api/routes/v1/projects.py` (CE router table); business logic in `core/services/project_service.py` and `project_file_service.py`.

| Method | Path | Description |
|---|---|---|
| GET | `/v1/projects` | List (personal + visible team projects) |
| POST | `/v1/projects` | Create (`kind=personal\|team`; linked folder can be specified or auto-created) |
| GET | `/v1/projects/teams` | Teams in which the user can create team projects |
| GET / PATCH / DELETE | `/v1/projects/{id}` | Detail / rename, description, pin, icon, instructions / soft delete |
| POST / DELETE | `/v1/projects/{id}/favorite` | Star / unstar |
| GET | `/v1/projects/{id}/files` | Project files (recursive linked-folder subtree) |
| POST | `/v1/projects/{id}/files/upload` | Direct upload (`filename` may contain a path; subfolders auto-created) |
| DELETE | `/v1/projects/{id}/files/{artifact_id}` | Soft delete (synced with MySpace) |
| PATCH | `/v1/projects/{id}/instructions` | Update project instructions |
| GET | `/v1/projects/{id}/chats` | Conversations inside the project (team projects show shared chats) |

### How project context enters a conversation

When a conversation is started inside a project (the request carries `project_id`), `api/routes/v1/chats.py` assembles the workflow context:

1. Project metadata is loaded — `project_name`, `project_instructions`, the linked folder name and file inventory;
2. `core/llm/agent_factory.py` injects these into the system prompt via `_build_project_section` (`build_system_prompt(cfg, ctx=...)`);
3. The project-level memory scope takes effect: the workspace becomes `project:<project_id>`; team projects use the mem0 bucket `team:<team_id>` (members share memories), and the project's own `metadata.memory_enabled` / `memory_write_enabled` override the user-level toggles (defaulting to on inside projects) — see [Memory System](./memory.md);
4. Sandbox path scoping: in project conversations, the agent's `/myspace/...` file operations are redirected under the linked folder (the explicit `ProjectScope` parameter mechanism in `core/llm/tools/myspace_vfs.py`).

Team project permissions follow team roles: owner/admin always have admin rights; members are governed by `file_permission` (editor/viewer) (`core/auth/permissions_iface.py::require_project_access`).

## MySpace

### File assets and personal folders

- Asset list: `GET /v1/artifacts` (`api/routes/v1/artifacts.py`), filterable by type / source / folder;
- Delete: `DELETE /v1/artifacts/{artifact_id}` (soft delete);
- Add to knowledge base: `POST /v1/artifacts/{artifact_id}/knowledge-base` (pairs with the system-managed "MySpace sync knowledge base", see [Knowledge Base](./knowledge-base.md));
- Personal folder tree: `src/backend/api/routes/v1/myspace_folders.py` —

| Method | Path | Description |
|---|---|---|
| GET | `/v1/myspace/folders` | Folder list |
| GET | `/v1/myspace/folders/breadcrumb` | Breadcrumb path |
| POST | `/v1/myspace/folders` | Create |
| PATCH / DELETE | `/v1/myspace/folders/{folder_id}` | Rename, move / cascading delete (with an affected-count pre-check endpoint) |
| POST | `/v1/myspace/folders/move-artifact` | Move a file into a folder |

File uploads go through the unified `POST /v1/file/upload` (with optional `folder_id` to land in a specific folder); see [Object Storage](./storage.md) for the storage path.

### Chat favorites

"Favorites" star **conversations**: the `ChatSession.favorite` flag, listed via `GET /v1/artifacts/favorites` (`api/routes/v1/artifacts.py`). Projects have their own separate `project_favorites` star mechanism.

### Frontend

`src/frontend/src/components/myspace/MySpacePanel.tsx` has four tabs: **File assets** (assets), **Chat favorites** (favorites), **Shares** (shares), and **Notifications** (notifications). Subcomponents:

- `DocumentList.tsx` / `ImageGrid.tsx` / `FavoriteList.tsx` / `NotificationList.tsx` / `ResourceCard.tsx`;
- `personal/`: personal folder creation and move modals;
- `team/`: team scope tree, breadcrumb, move-to-team, permission modals (Enterprise Edition, EE);
- State: `stores/mySpaceStore.ts`.

Project frontend lives in `src/frontend/src/components/projects/`: `ProjectsPanel` (list), `ProjectCard`, `CreateProjectModal`, `ProjectDetailPanel` (files + instructions + chats), `ProjectRightRail`, `ProjectMemoriesModal` (project memory viewer); state in `stores/projectStore.ts`.

## Team folders and team files (Enterprise Edition, EE)

User-facing routes are in `src/backend/edition_ee/routes/team_files.py`, gated by the `multi_tenancy` feature flag (EE router table); the admin counterpart is `/v1/config/teams/*` (`edition_ee/routes/config_teams.py`).

| Method | Path | Description |
|---|---|---|
| GET | `/v1/my-teams` | My teams |
| GET / POST | `/v1/teams/{team_id}/folders` | Team folder list / create |
| PATCH / DELETE | `/v1/teams/{team_id}/folders/{folder_id}` | Rename / delete |
| GET / POST | `/v1/teams/{team_id}/files[, /upload]` | Team file list / upload |
| DELETE / POST | `/v1/teams/{team_id}/files/{artifact_id}[, /move]` | Delete / move |
| POST | `/v1/artifacts/{artifact_id}/move-to-team` | Convert a personal file into a team file |
| GET / PUT | `/v1/teams/{team_id}/members/permissions`, `.../{user_id}/permission` | View / adjust member file permissions |

Permission model: `TeamMember.role` (owner/admin/member) + `file_permission` (viewer/editor, effective only for members), encapsulated in the EE-only `edition_ee/auth/team_permissions.py`. Team files have a dedicated shared sandbox cache, `team_cache_dir(team_id)`, reused across members of the same team.

## How files enter conversation context

Three complementary paths:

1. **Attachment injection (hooks)**: when the user uploads a file or picks one from MySpace, the request's `attachments[].file_id` is processed by the pre_reply hooks in `core/llm/hooks.py` — `_build_file_context()` pulls `parsed_text` (downloading and parsing from object storage on a miss) and injects the assembled context text; a 50K-character budget per file, with truncation plus a hint to continue paginated reading via `read_artifact`; xlsx files take a dedicated "summary + preview + operating guidance" branch to avoid truncation misleading the model; images go through the multimodal injection branch. Every `file_id` fetch validates `user_id` ownership to block forged cross-user reads;
2. **Project file inventory**: a project conversation's system prompt carries the linked folder's file list (see above); the agent reads specific contents on demand via `read_artifact` or sandbox tools;
3. **Sandbox virtual filesystem**: with code execution enabled, the `/myspace/...` path maps MySpace into the sandbox (lazy loading + reverse sync); team projects map the team folder — see [Sandbox](./sandbox.md).

## Source map

| Path | Responsibility |
|---|---|
| `src/backend/api/routes/v1/projects.py` | Projects API |
| `src/backend/core/services/project_service.py` / `project_file_service.py` | Project business logic |
| `src/backend/core/services/project_scope.py` | `ProjectScope` (sandbox path scoping) |
| `src/backend/api/routes/v1/myspace_folders.py` | Personal folders API |
| `src/backend/api/routes/v1/artifacts.py` | Asset list / chat favorites / add-to-KB |
| `src/backend/edition_ee/routes/team_files.py` | Team folders & files API (Enterprise Edition, EE) |
| `src/backend/api/routes/v1/file_upload.py` | File upload (folder targeting) |
| `src/backend/core/db/models/project.py` | `Project` / `ProjectFavorite` ORM |
| `src/backend/core/db/models/identity.py` | Shared identity ORM such as `UserFolder` |
| `src/backend/edition_ee/db/models/identity.py` | `Team` / `TeamMember` / `TeamFolder` ORM (EE only) |
| `src/backend/core/db/models/artifact.py` | `Artifact` ORM |
| `src/backend/core/llm/hooks.py` | Attachment context injection (`_build_file_context`, etc.) |
| `src/backend/core/llm/agent_factory.py` | Project section injection into the system prompt |
| `src/backend/core/llm/tools/myspace_vfs.py` | MySpace ↔ sandbox mapping layer |
| `src/frontend/src/components/projects/` | Project frontend components |
| `src/frontend/src/components/myspace/` | MySpace frontend components |

Related docs: [Memory System](./memory.md) · [Object Storage](./storage.md) · [Sandbox](./sandbox.md) · [Knowledge Base](./knowledge-base.md) · [Auth & Teams](./auth.md) · [Edition Comparison](../editions/overview.md)
