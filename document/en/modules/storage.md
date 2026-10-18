# Object storage

Cloud Docker deployments in CE and EE default to S3-compatible storage. Desktop offline mode and the one-command local installation keep local files.

Set STORAGE_TYPE=s3 and configure S3_ENDPOINT, S3_BUCKET, S3_REGION, S3_ACCESS_KEY and S3_SECRET_KEY. The backend and sandbox controller must reach the endpoint.

The optional docker/compose.object-storage.yml starts an independent S3 service. It uses its own volume and does not publish host ports. It does not share Milvus storage.

CE initialization generates separate random access and secret keys. Run python3 scripts/configure-ce-docker.py in a new CE checkout. It refuses to overwrite an existing .env.

Build the backend image before starting the bucket initializer. Run sudo bash docker/prepare-object-mounts.sh with the absolute host storage path. Then start the services.

Existing installations must back up configuration, databases and file references before changing providers. Copy required objects and verify their SHA-256 values. Preserve the source objects.

The storage factory is core/storage/factory.py. S3StorageBackend uses boto3 and includes directory operations. LocalStorageBackend serves desktop local files. OSS and OBS adapters remain EE providers.

The artifact store supports S3, OSS and OBS. It keeps local metadata records and mirrors them into object storage. Historical download references remain valid when migrated objects keep their storage keys.

My Space reads the authorized directory directly. Cloud sandboxes mount the same object prefix. They do not depend on a background mirror or file-registration watcher.

Directory moves contain multiple object operations and are not atomic. See the space-files deployment guide for import, mount recovery and permission checks.

[Space files](../deployment/space-files.md)
