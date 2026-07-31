# Scaleway Glacier

Mnema supports Scaleway Object Storage as an encrypted archival S3 boundary.

## Safety sequence

1. Stream-encrypt the active file with AES-256-GCM.
2. Upload to the existing private bucket in Standard storage.
3. Independently download, decrypt, and verify the plaintext SHA-256.
4. Commit the verified state.
5. Change the verified object to `GLACIER` using an idempotent server-side copy.
6. Confirm `StorageClass=GLACIER` before recording the archived state.

This ordering prevents an inaccessible Glacier object from being accepted without an
independent restore verification. Large encrypted objects use multipart server-side
copy, and incomplete multipart transitions are aborted on failure.

## Configuration

Glacier is available in the Paris and Amsterdam regions:

```bash
sudo mnema configure cold-storage \
  --provider scaleway \
  --region fr-par \
  --s3-bucket YOUR_EXISTING_BUCKET \
  --s3-access-key-file /safe/input/access-key \
  --s3-secret-key-file /safe/input/secret-key \
  --yes
```

Create the private bucket and scoped IAM credentials in Scaleway first. Mnema does not
create or delete cloud buckets. Credentials must permit bucket inspection, object
upload/download/head, same-key copy and multipart-copy operations, and `RestoreObject`.
They do not need object or bucket deletion permission.

Scaleway references:

- https://www.scaleway.com/en/docs/object-storage/concepts/
- https://www.scaleway.com/en/docs/object-storage/how-to/restore-an-object-from-glacier/
- https://www.scaleway.com/en/docs/object-storage/api-cli/object-operations

## Restore behavior

Glacier retrieval is asynchronous and may take minutes to 24 hours. A remote restore
attempt submits the restore request once and records a pending audit result. Repeating
the restore while retrieval is active does not submit another request. Once Scaleway
reports retrieval complete, Mnema downloads, decrypts, and verifies the restored
plaintext.

No lifecycle expiration or cloud deletion is configured.
