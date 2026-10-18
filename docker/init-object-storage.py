"""Create the configured bucket without changing existing objects."""
import os
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

def main():
    bucket = os.environ["S3_BUCKET"]
    client = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"],
        region_name=os.getenv("S3_REGION", "us-east-1"),
        aws_access_key_id=os.environ["S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["S3_SECRET_KEY"],
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                      connect_timeout=5, read_timeout=15, retries={"max_attempts": 3}))
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as exc:
        if str(exc.response["Error"]["Code"]) not in ("404", "NoSuchBucket"):
            raise RuntimeError("Cannot access the configured S3 bucket") from None
        args = {"Bucket": bucket}
        region = os.getenv("S3_REGION", "us-east-1")
        if region != "us-east-1":
            args["CreateBucketConfiguration"] = {"LocationConstraint": region}
        client.create_bucket(**args)
    client.head_bucket(Bucket=bucket)
    print("Object storage bucket is ready.")

if __name__ == "__main__":
    main()
