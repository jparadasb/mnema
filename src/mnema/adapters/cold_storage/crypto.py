from __future__ import annotations

import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b"MNEMA1"
NONCE_SIZE = 12
TAG_SIZE = 16


def encrypt_file(source: Path, destination: Path, key: bytes) -> None:
    if len(key) != 32:
        raise ValueError("cold encryption key must contain exactly 32 bytes")
    nonce = os.urandom(NONCE_SIZE)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    with source.open("rb") as reader, destination.open("xb") as writer:
        writer.write(MAGIC)
        writer.write(nonce)
        while chunk := reader.read(1024 * 1024):
            writer.write(encryptor.update(chunk))
        writer.write(encryptor.finalize())
        writer.write(encryptor.tag)


def decrypt_file(source: Path, destination: Path, key: bytes) -> None:
    size = source.stat().st_size
    if size < len(MAGIC) + NONCE_SIZE + TAG_SIZE:
        raise ValueError("encrypted object is truncated")
    with source.open("rb") as reader:
        if reader.read(len(MAGIC)) != MAGIC:
            raise ValueError("encrypted object header is invalid")
        nonce = reader.read(NONCE_SIZE)
        reader.seek(-TAG_SIZE, os.SEEK_END)
        tag = reader.read(TAG_SIZE)
        reader.seek(len(MAGIC) + NONCE_SIZE)
        remaining = size - len(MAGIC) - NONCE_SIZE - TAG_SIZE
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        with destination.open("xb") as writer:
            while remaining:
                chunk = reader.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("encrypted object ended unexpectedly")
                writer.write(decryptor.update(chunk))
                remaining -= len(chunk)
            writer.write(decryptor.finalize())


def sha256_hex(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
