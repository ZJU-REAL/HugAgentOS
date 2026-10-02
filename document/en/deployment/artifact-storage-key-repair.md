# Repair historical artifact storage keys

Older releases could register `artifacts/<id>.docx` objects using the placeholder
`artifacts/<id>`. Generated-file persistence now requires a real storage key.
Personal-space metadata reads drain local writes without requiring every remote
file to download. Import failures show an error and retry control instead of an empty list.

## Safety

Repair matches only `storage_key == artifacts/<artifact_id>`. It requires confirmed
absence of that original object, an existing object with the recorded filename suffix,
and downloaded bytes matching the recorded size. Timeouts, authorization errors,
missing replacements and size mismatches leave the record unchanged. Existing
extensionless objects are preserved.

Updates compare the original key, filename, size and content timestamp atomically.
They preserve the content version. The old/new keys, SHA-256 and original content
version are committed together in `metadata.storage_key_repair` for audit and
conditional rollback. Failed personal-space projection uses the same verified repair;
existing conflict protection preserves unsaved local edits.

## Operation

Follow repository release approval rules. Validate in staging first and obtain
separate approval before production code or data changes. Run from the project root
using the authorized environment runtime and configuration. Never copy credentials
or `.env` files between environments.

```bash
# Default: verify and print candidates without changing database records.
PYTHONPATH=src/backend python scripts/repair_artifact_storage_keys.py --user-id <USER_ID>

# Apply after reviewing candidates; already repaired records are skipped.
PYTHONPATH=src/backend python scripts/repair_artifact_storage_keys.py --user-id <USER_ID> --apply

# Undo audited repairs only while the current key and content version are unchanged.
# This restores the old key. An upgraded background projector may repair it again.
PYTHONPATH=src/backend python scripts/repair_artifact_storage_keys.py --user-id <USER_ID> --rollback
```

The tool processes one explicitly selected account in ID pages and exits nonzero on
errors. It includes historical deleted records without restoring files or changing
folder ownership.

## Verification

1. Confirm candidate keys, object sizes and audit records; repeat runs make no duplicate changes.
2. Verify successful root/folder listing APIs and downloads of valid files.
3. In Windows desktop local mode, select a file using both `@` and `+ → Import from My Space`, then send it and verify the document content is readable.
4. With one unavailable object, other metadata remains accessible and fetching the affected file reports failure.
5. Failed requests expose retry; old responses cannot overwrite a newer filter or folder.

Frontend changes require a desktop client release to affect installed clients.
A successful source build does not imply publication or production data changes.
Record staging, production and desktop verification separately.
