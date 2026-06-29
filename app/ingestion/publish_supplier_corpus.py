"""
Publish the supplier knowledge corpus to an S3 data source bucket.

Transforms supplier_profiles.json into individually addressable, metadata-tagged
documents suitable for ingestion by any RAG-capable retrieval system. Makes no
Bedrock API calls — publishing and retrieval-system synchronization are separate
operational responsibilities.

Usage:
    python -m app.ingestion.publish_supplier_corpus

Configuration (environment variables):
    CORPUS_S3_BUCKET  — target S3 bucket (required)
    CORPUS_S3_PREFIX  — key prefix inside the bucket (default: supplier-profiles/)
    AWS_DEFAULT_REGION — AWS region for the S3 client (default: us-west-2)

After publishing, trigger Knowledge Base re-indexing separately:
    aws bedrock-agent start-ingestion-job \
        --knowledge-base-id $BEDROCK_KB_ID \
        --data-source-id $BEDROCK_KB_DATA_SOURCE_ID
"""

import json
import os
import sys
from pathlib import Path

import boto3

_PROFILES_PATH = Path(__file__).parent.parent / "storage" / "supplier_profiles.json"


def publish(
    profiles_path: Path,
    bucket: str,
    prefix: str,
    region: str,
) -> list[str]:
    """Upload supplier profiles to S3 as text files with metadata sidecars.

    Returns the list of S3 keys written.
    """
    with open(profiles_path, encoding="utf-8") as f:
        profiles = json.load(f)

    s3 = boto3.client("s3", region_name=region)
    written: list[str] = []

    for entry in profiles:
        supplier = entry["supplier"]
        profile_text = entry["profile"]

        doc_key = f"{prefix}{supplier}.txt"
        meta_key = f"{doc_key}.metadata.json"
        metadata = {"metadataAttributes": {"supplier": supplier}}

        s3.put_object(Bucket=bucket, Key=doc_key, Body=profile_text.encode("utf-8"))
        s3.put_object(
            Bucket=bucket,
            Key=meta_key,
            Body=json.dumps(metadata).encode("utf-8"),
            ContentType="application/json",
        )
        written.extend([doc_key, meta_key])
        print(f"  uploaded s3://{bucket}/{doc_key}")
        print(f"  uploaded s3://{bucket}/{meta_key}")

    return written


def main() -> None:
    bucket = os.environ.get("CORPUS_S3_BUCKET")
    if not bucket:
        print("error: CORPUS_S3_BUCKET is not set", file=sys.stderr)
        sys.exit(1)

    prefix = os.environ.get("CORPUS_S3_PREFIX", "supplier-profiles/")
    region = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")

    print(f"Publishing supplier corpus to s3://{bucket}/{prefix}")
    keys = publish(_PROFILES_PATH, bucket, prefix, region)
    print(f"\nDone — {len(keys)} objects written.")
    print(
        "\nNext step: trigger Knowledge Base re-indexing:\n"
        "  aws bedrock-agent start-ingestion-job \\\n"
        "      --knowledge-base-id $BEDROCK_KB_ID \\\n"
        "      --data-source-id $BEDROCK_KB_DATA_SOURCE_ID"
    )


if __name__ == "__main__":
    main()
