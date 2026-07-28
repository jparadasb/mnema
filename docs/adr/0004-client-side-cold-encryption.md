# ADR 0004: Client-side authenticated cold encryption

Status: accepted

Encrypt cold objects before S3-compatible upload with AES-256-GCM and a per-object nonce. Verify by downloading, decrypting, and hashing plaintext.

Reason: object provider credentials or storage compromise must not reveal archive contents.

Consequence: key loss makes remote archives unrecoverable; secure key backup and rotation need a later workflow.

