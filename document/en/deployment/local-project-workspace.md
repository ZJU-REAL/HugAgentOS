# Local project working directory

When a desktop conversation is bound to a local project, Bash starts in the real project directory. Relative paths in Read, Write, Edit, Glob, Grep, file management tools and `pin_to_workspace(file_paths=[...])` use the same directory. Real absolute paths are also supported.

For a project at `/Users/alice/projects/industry-chain`, generating `deliverables/report.html`, `deliverables/nodes.xlsx` and `deliverables/delivery.zip` saves those files directly in the project. Pin creates references to the originals without moving or copying them; subsequent previews read the current originals. `deliverables/` is an organizational example, not a required directory.

Unbound conversations continue using `<local workspace>/.sessions/<session hash>/` as their persistent working directory. Bound conversations also retain that directory for execution scripts, process logs and temporary files. Closing a session cleans up session storage, never project contents. The environment prompt exposes `cwd` for execution and `session_scratch` for session storage.

If the project directory is deleted, moved or inaccessible, or the project binding changes, the old run fails explicitly. It does not fall back to session storage or create a replacement project directory. Restore the directory or rebind the project and start a new turn before retrying.

Changing cwd does not grant additional filesystem access. Existing directory grants, permission presets and OS confinement still apply. Relative paths, `..` and symbolic links are checked against their real targets. A `cd` inside one Bash invocation does not change the default directory of later calls.

Existing `.sessions` deliverables are not migrated automatically. Explicitly copy historical deliverables into the project and pin them there if needed. Skill source may live in the project; this behavior does not change skill installation, registration or capability-directory protocols. Cloud conversations keep their existing workspace rules.

Apply the `localcwd01` data migration when upgrading the local backend. It idempotently updates saved stock desktop wording while preserving active versions, custom text and cloud prompts. Administrators should review any independently customized cwd instructions.
