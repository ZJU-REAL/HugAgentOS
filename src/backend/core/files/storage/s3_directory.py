"""S3 directory operations shared by S3-compatible services."""
from core.files.storage.entries import DirectoryPage, StorageEntry, page_limit, validate_key

class S3DirectoryOperations:
    def list_directory(self, prefix, *, cursor=None, limit=200):
        prefix = validate_key(prefix, directory=True)
        args = dict(Bucket=self.bucket, Prefix=prefix, Delimiter="/", MaxKeys=page_limit(limit))
        if cursor:
            args["ContinuationToken"] = cursor
        result = self.s3_client.list_objects_v2(**args)
        entries = [
            StorageEntry(item["Prefix"][len(prefix):].rstrip("/"), item["Prefix"], True)
            for item in result.get("CommonPrefixes", [])
        ]
        for item in result.get("Contents", []):
            key = item["Key"]
            if key == prefix:
                continue
            entries.append(StorageEntry(key[len(prefix):], key, False, item["Size"],
                item.get("LastModified"), item.get("ETag")))
        return DirectoryPage(tuple(sorted(entries, key=lambda e: (not e.is_directory, e.name))),
            result.get("NextContinuationToken") if result.get("IsTruncated") else None)

    def stat_object(self, key):
        key = validate_key(key, directory=key.endswith("/"))
        try:
            result = self.s3_client.head_object(Bucket=self.bucket, Key=key)
        except self.ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return StorageEntry(key.rstrip("/").rsplit("/", 1)[-1], key, key.endswith("/"),
            result["ContentLength"], result.get("LastModified"), result.get("ETag"),
            result.get("VersionId"))

    def make_directory(self, prefix):
        self.s3_client.put_object(Bucket=self.bucket,
            Key=validate_key(prefix, directory=True), Body=b"")

    def copy_object(self, source, destination):
        self.s3_client.copy(
            {"Bucket": self.bucket, "Key": validate_key(source)},
            self.bucket, validate_key(destination),
        )

    def remove_directory(self, prefix):
        prefix = validate_key(prefix, directory=True)
        cursor = None
        while True:
            page = self.list_directory(prefix, cursor=cursor, limit=2)
            if page.entries:
                raise OSError("Directory is not empty")
            if not page.next_cursor:
                break
            cursor = page.next_cursor
        self.s3_client.delete_object(Bucket=self.bucket, Key=prefix)
