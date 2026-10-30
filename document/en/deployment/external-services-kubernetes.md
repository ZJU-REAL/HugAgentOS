# Kubernetes with external services (EE)

Use an existing PostgreSQL database, Redis service, and MinIO bucket. This deployment does not create databases, buckets, persistent volumes, CSI, or internal storage services. It excludes Personal Agent, LiteLLM/model gateway, internal knowledge-base services, and snapshot-committer. Memory uses pgvector in the same PostgreSQL database. Configure reachable model and embedding endpoints with the correct embedding dimensions.

## Platform requirements

- An existing namespace, registry pull secret, ingress domain and TLS secret; images pinned by digest for the target architecture.
- Kubernetes 1.29 or newer with native sidecar support, installed OpenSandbox CRDs, and permission to manage namespaced workloads, service accounts, RBAC, secrets and network policies.
- An existing PostgreSQL database with the vector extension installed. The owner account must be able to run Alembic migrations and read/write business tables.
- Authenticated Redis access and MinIO permissions to list, read, write and delete within the application prefix.
- A CNI that actually enforces NetworkPolicy. Sandbox sessions must not reach another session's execution or desktop endpoints.
- A controlled privileged helper with FUSE and Bidirectional mount propagation. The code execution container remains unprivileged; only the helper receives storage credentials.
- Platforms that forbid privileged containers set `HUGAGENT_SPACE_MOUNT_MODE=sync` (renderer: `space_transport: "sync"`). Nothing is mounted; the backend synchronizes space files over the sandbox's HTTP file API before and after each command. No privileged helper or FUSE is needed; consistency holds at command boundaries. See `internal design docs`.

Ordinary Pod permissions alone cannot satisfy live filesystem mounts if the platform prohibits the helper capabilities. The deployment rejects missing mounts rather than substituting downloaded copies.

## Configuration without an in-container .env

The deployment tooling lives on the deploy branch at deploy_kit/kubernetes/external.py. Initialize inputs in a private directory outside the repository:

```bash
python3 deploy_kit/kubernetes/external.py init --directory /secure/hugagent-input
```

For an external-services image pack, use registry-site.json containing registry, release and architecture:

```bash
python3 deploy_kit/kubernetes/cloud.py import-images --external-services \
  --directory KIT --config registry-site.json --output images.json
```

For browser upload, use console-images --external-services. Copy the resulting image mapping into site.json, checking digests and architecture. Fill site.json with non-secret service settings and secrets.json with credentials and stable internal keys. Use separate strong values for ADMIN_TOKEN, CONFIG_TOKEN, BACKEND_INTERNAL_TOKEN, OPENSANDBOX_API_KEY and PLUGIN_RESOURCE_SECRET_KEY. Preserve them on upgrades. Enter PostgreSQL and Redis as the separate fields the administrator returns: DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD and REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_DB, optionally DB_SSLMODE (disable, allow, prefer or require; CA-verifying modes are not supported yet), REDIS_TLS (true/false) and REDIS_USERNAME. The renderer encodes special characters and builds DATABASE_URL and REDIS_URL; complete URLs are still accepted. The application does not prefix Redis keys, so use a dedicated Redis database number that no other system shares. Do not embed credentials in images or command arguments.

```bash
python3 deploy_kit/kubernetes/external.py render \
  --config /secure/hugagent-input/site.json \
  --secrets /secure/hugagent-input/secrets.json \
  --license /secure/license.lic \
  --output /secure/hugagent-rendered
```

The generated 00-config.json contains secrets. Keep the directory private (0700) and files restricted (0600). Containers receive ConfigMap/Secret values through envFrom; PYTHON_DOTENV_DISABLED=1 prevents dotenv loading. The renderer sets DEPLOY_PROFILE=kubernetes, STORAGE_TYPE=s3, SESSION_STORE=redis, MEM0_VECTOR_PROVIDER=pgvector, HUGAGENT_SPACE_MOUNT_MODE=sidecar, MODEL_GATEWAY_ENABLED=false and PERSONAL_AGENT_ENABLED=false. Use the renderer's complete configuration rather than this abbreviated list.

The license Secret is a read-only seed. Verified renewals and clock state persist in PostgreSQL. Each process re-reads them every LICENSE_DB_REFRESH_SECONDS (default 30); requests do not query the database. During a short outage the last confirmed license stays valid for up to LICENSE_DB_OUTAGE_GRACE_SECONDS (default 300); a cold start or a longer outage fails closed. Artifact contents and metadata, manuals and branding assets persist in object storage; catalog enablement and ordering persist in PostgreSQL. Re-render after configuration changes: the configuration digest triggers Pod replacement. Do not edit live container files.

## Preflight and installation order

Back up existing data before upgrades and select the correct Kubernetes context. Preflight writes configuration and temporary probes into the target namespace. It checks real PG vector operations, Redis writes/reads, MinIO writes/reads, FUSE read/write propagation, read-only mounts and credential isolation. It also tests trusted access, denied cross-session access, then trusted access again using direct Pod IPs. Probe resources are cleaned up; the isolation policy and empty audit marker directories remain. It does not create a database, bucket or storage volume.

```bash
python3 deploy_kit/kubernetes/external.py preflight \
  --directory /secure/hugagent-rendered --context TARGET_CONTEXT
```

Only proceed after all probes pass. Apply 03-control.json, then 04-migrate.json and wait for the migration Job to complete. Stop on migration failure. Apply 05-applications.json, 06-services.json and 07-ingress.json, then wait for backend and mcp rollouts. For example, replacing the namespace and release-specific Job name:

```bash
kubectl --context TARGET_CONTEXT -n hugagent apply -f /secure/hugagent-rendered/03-control.json
kubectl --context TARGET_CONTEXT -n hugagent apply -f /secure/hugagent-rendered/04-migrate.json
kubectl --context TARGET_CONTEXT -n hugagent wait --for=condition=complete job/migrate-external-01 --timeout=1800s
kubectl --context TARGET_CONTEXT -n hugagent apply -f /secure/hugagent-rendered/05-applications.json
kubectl --context TARGET_CONTEXT -n hugagent apply -f /secure/hugagent-rendered/06-services.json
kubectl --context TARGET_CONTEXT -n hugagent apply -f /secure/hugagent-rendered/07-ingress.json
kubectl --context TARGET_CONTEXT -n hugagent rollout status deployment/backend --timeout=600s
kubectl --context TARGET_CONTEXT -n hugagent rollout status deployment/mcp --timeout=600s
```

Use a new release for each deployment; do not replace an active migration Job. Browser console uploads can follow these groups, but uploading static probe Jobs cannot replace the dynamic network preflight. An administrator or workstation with cluster access must run it.

## Runtime behavior and acceptance

Each session has a separate temporary execution directory. The same user's sessions share authorized persistent My Space files through live mounts. Team mounts follow existing authorization and read-only grants. Mount loss stops execution. Skills are checked against the current authorized view before each command and re-sent when the view changes or the sandbox is reset for a new session, including relative-path execution and removal of disabled/deleted packages. Revocation does not erase copies that a user previously saved elsewhere. Host-backed third-party credential directories and desktop capability publication are unsupported in this Kubernetes mode and fail explicitly.

Without persistent volumes, control-plane SQLite state is temporary. Snapshots are disabled; no snapshot registry or node runtime socket is needed. Do not assume all active task state survives control-plane replacement. Persistent space files remain in object storage.

Local isolated tests cover real database authorization, Bash execution, session isolation, live space sharing, private skill execution/revocation, read-only mounts and mount failure/recovery. These results do not establish target-platform permissions or model availability. Before release, test login, a real model reply, code in two sessions, cross-user isolation, persistent file recovery after Pod replacement and network isolation on the target platform.

## Source builds

Build backend, mcp and frontend from this source and supply their digests to the deploy branch renderer. Use the matching patched sandbox server and space-mount helper. The source branch includes shared mount helpers, not the full delivery CLI. Preserve the current source branch admission and session runtime. Rebuild affected images: already deployed images do not contain uncommitted source changes.
