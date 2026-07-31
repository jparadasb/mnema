# ADR 0006: Selectable rclone cold transport

Status: accepted

Mnema supports rclone as a subprocess boundary for encrypted cold-object transport while
retaining the direct S3 adapter for provider-specific integration tests. Mnema encrypts
with AES-256-GCM before invoking rclone, uses an idempotency-derived immutable object
name, independently observes remote metadata, and verifies by copy-back, decryption, and
plaintext SHA-256.

rclone receives an argument array and a root-owned configuration file; credentials never
appear in command arguments or error messages. Non-not-found stat failures remain fatal.
Persisted receipts are constrained to the configured remote root before restore.
