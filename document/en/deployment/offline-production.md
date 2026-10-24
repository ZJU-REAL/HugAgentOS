# Offline Production Deployment (Enterprise Edition)

> Last updated: 2026-10-16 ｜ [简体中文](../../zh-CN/deployment/offline-production.md) ｜ Back to [Deployment Guide](README.md)

Offline delivery uses compiled images and the matching deployment kit from the `deploy` branch.
Build and package in the supplier's separate `deploy` checkout. Load the images and start the matching kit on the target.
Branch synchronization, builds, uploads and production publication each follow the project authorization rules.

## Connected side: prepare the complete delivery

1. Verify the validated `deploy` commit, clean checkout, product identity and target CPU architecture.
2. Read that commit's `deploy_kit/save_pack_ee.sh` and matching Compose files in full.
3. Build and package:

   ```bash
   # Run at the root of the deploy checkout
   bash deploy_kit/save_pack_ee.sh --build
   # Package existing compiled images only after checking their source revision
   bash deploy_kit/save_pack_ee.sh
   ```

The script checks compiled backend, MCP, frontend and required infrastructure images, then includes available optional images.
The supplier must also verify every dependency of the target profiles. An absent optional image does not mean the target does not need it.
Use image names, versions, output paths and filenames from the selected script and manifest without renaming them.
Deliver the image archive, manifest and matching kit together. Deliver the License separately through a secure channel.
Ordinary source images or an incremental backend/frontend pack cannot replace the complete EE delivery.

## Target side: load and start

Follow the delivered kit to configure a separate project name, data directories, License and protected environment files.
Before upgrading, back up the database and files, and verify the manifest, CPU architecture and selected profiles.
After authorization, load the specified archive and use the matching kit's startup entry and complete Compose overlays.
For targets without registry access, verify that all required images are loaded and startup does not build images.
Do not use the former source-deployment scripts or automatically select an unverified archive by modification time.

Verify at least `/health`, `/ready`, login, attachments, model calls and the enabled sandbox capabilities.
Database migrations modify the schema; persistent volumes do not mean upgrades cannot change business data.
On failure, restore the recorded images, configuration and data backups without deleting data volumes.

## Shipping prompt snapshots with the pack

System prompts live in the database `content_blocks` table (the `prompt_versions` pool plus the `prompt_hub` template gallery). They are **not in the repository and not in the images**. To push new prompts to production, ship them as a separate data file alongside the image pack and import on the production side:

### 1. Export on the connected side

Export a snapshot through `GET /v1/content/prompts/export` using the source environment's
administrator or system configuration token. Save the response's `data` object as
`prompts_snapshot_<ts>.json`; do not save the complete response envelope.

### 2. Copy

Copy `prompts_snapshot_<ts>.json` to the production machine together with the image tarball.

### 3. Import on the production side

Prerequisite: the production backend image already includes the `POST /v1/content/prompts/import` endpoint (`src/backend/api/routes/v1/content.py`). Once the new backend is up:

```bash
BACKEND_CONTAINER=your-backend-container
docker cp prompts_snapshot_<ts>.json "${BACKEND_CONTAINER}:/tmp/"
docker exec "$BACKEND_CONTAINER" curl -sS -X POST \
  'http://localhost:3001/v1/content/prompts/import?overwrite=true' \
  -H "Authorization: Bearer <production ADMIN_TOKEN>" \
  -H 'Content-Type: application/json' \
  -d @/tmp/prompts_snapshot_<ts>.json
```

The API import invalidates the prompt cache automatically — **no backend restart needed**.

> This is an on-demand, one-time data operation: the DB volume persists, so prompts do not need re-importing on every release — only when pushing new prompts to production.

## Related source

| Feature | Files |
|---|---|
| Complete EE image pack (deploy branch) | `deploy_kit/save_pack_ee.sh` |
| Compiled EE images (deploy branch) | `docker/Dockerfile.backend-compiled`, `docker/Dockerfile.mcp-compiled` |
| Compose overlays (deploy branch) | `docker-compose.prod.yml`, `docker-compose.deploy.yml`; runtime follows the delivered kit |
| Prompt export / import | `src/backend/api/routes/v1/content.py` |
| Prompt version pool | `src/backend/core/llm/prompt_versions.py` |
