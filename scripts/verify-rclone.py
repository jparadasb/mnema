#!/usr/bin/env python3
"""Verify encrypted rclone cold storage against a disposable test remote."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from mnema.adapters.cold_storage.crypto import sha256_hex
from mnema.adapters.cold_storage.rclone import RcloneEncryptedColdStorage

MIB = 1024 * 1024


def write_proof(path: Path, size: int) -> str:
    digest = hashlib.sha256()
    block = hashlib.sha256(b"mnema-rclone-proof").digest() * (MIB // 32)
    remaining = size
    with path.open("xb") as output:
        while remaining:
            chunk = block[: min(remaining, len(block))]
            output.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        output.flush()
        os.fsync(output.fileno())
    return digest.hexdigest()


async def verify(args: argparse.Namespace) -> dict[str, Any]:
    key = args.key_file.read_bytes()
    storage = RcloneEncryptedColdStorage(
        remote_root=args.remote_root,
        config_file=args.config_file,
        key=key,
    )
    with tempfile.TemporaryDirectory(prefix="mnema-rclone-proof-", dir=args.temporary_root) as root:
        directory = Path(root)
        source = directory / "source.bin"
        restored = directory / "restored.bin"
        expected_hash = write_proof(source, args.bytes)
        receipt = await storage.upload(source, source.name, "rclone-proof")
        repeated = await storage.upload(source, source.name, "rclone-proof")
        independently_verified = await storage.verify(receipt, expected_hash)
        await storage.restore(receipt, restored)
        restored_hash = sha256_hex(restored)
        listing = json.loads(
            await storage._run(
                "lsjson",
                "--recursive",
                "--files-only",
                f"{args.remote_root.rstrip('/')}/mnema",
            )
        )
    if receipt != repeated:
        raise RuntimeError("rclone idempotent upload returned a different receipt")
    if not independently_verified or restored_hash != expected_hash:
        raise RuntimeError("rclone independent restore verification failed")
    if not isinstance(listing, list) or len(listing) != 1:
        raise RuntimeError("rclone remote object count was not exactly one")
    return {
        "provider": receipt.provider,
        "remote_size": receipt.remote_size,
        "remote_checksum_present": receipt.remote_checksum is not None,
        "idempotent_receipt": receipt == repeated,
        "independent_restore_verified": independently_verified,
        "remote_objects": len(listing),
        "plaintext_bytes": args.bytes,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-file", required=True, type=Path)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--remote-root", required=True)
    parser.add_argument("--temporary-root", type=Path)
    parser.add_argument("--bytes", type=int, default=8 * MIB)
    args = parser.parse_args()
    if not args.config_file.is_file() or not args.key_file.is_file():
        parser.error("config and key files must exist")
    if args.temporary_root is not None and not args.temporary_root.is_dir():
        parser.error("temporary root must exist")
    if args.bytes < 1:
        parser.error("bytes must be positive")
    return args


def main() -> None:
    print(json.dumps(asyncio.run(verify(parse_args())), indent=2))


if __name__ == "__main__":
    main()
