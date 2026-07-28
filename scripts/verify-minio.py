#!/usr/bin/env python3
"""Run a non-destructive encrypted MinIO upload and restore proof."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import boto3

from mnema.adapters.cold_storage.crypto import sha256_hex
from mnema.adapters.cold_storage.s3 import S3EncryptedColdStorage


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("restore_destination", type=Path)
    parser.add_argument("--endpoint", default="http://minio:9000")
    parser.add_argument("--bucket", default="mnema-integration")
    parser.add_argument("--user-file", type=Path, default=Path("/run/secrets/minio_user"))
    parser.add_argument(
        "--password-file",
        type=Path,
        default=Path("/run/secrets/minio_password"),
    )
    parser.add_argument(
        "--encryption-key-file",
        type=Path,
        default=Path("/run/secrets/mnema_cold_key"),
    )
    return parser.parse_args()


async def run() -> None:
    args = arguments()
    access_key = args.user_file.read_text(encoding="utf-8").strip()
    secret_key = args.password_file.read_text(encoding="utf-8").strip()
    encryption_key = args.encryption_key_file.read_bytes()
    client = boto3.client(
        "s3",
        endpoint_url=args.endpoint,
        region_name="us-east-1",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )
    buckets = {entry["Name"] for entry in client.list_buckets().get("Buckets", [])}
    if args.bucket not in buckets:
        client.create_bucket(Bucket=args.bucket)

    expected_sha256 = sha256_hex(args.source)
    storage = S3EncryptedColdStorage(
        bucket=args.bucket,
        key=encryption_key,
        client=client,
    )
    receipt = await storage.upload(
        args.source,
        object_identifier=args.source.name,
        idempotency_key=f"rpi-proof-{expected_sha256[:16]}",
    )
    independently_verified = await storage.verify(receipt, expected_sha256)
    args.restore_destination.parent.mkdir(parents=True, exist_ok=True)
    await storage.restore(receipt, args.restore_destination)
    restored_sha256 = sha256_hex(args.restore_destination)
    if not independently_verified or restored_sha256 != expected_sha256:
        raise RuntimeError("MinIO restore verification failed")

    print(
        json.dumps(
            {
                "bucket": receipt.bucket,
                "encryption_mode": receipt.encryption_mode,
                "object_identifier": receipt.object_identifier,
                "remote_size": receipt.remote_size,
                "source_sha256": expected_sha256,
                "restored_sha256": restored_sha256,
                "verified": True,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(run())
