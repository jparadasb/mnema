# Stress and failure testing

`scripts/stress-test.py` exercises only generated data inside Python-managed temporary
directories. It never enables or calls source deletion. Every successful scale case
independently restores both the first and last item from local and encrypted cold copies.

Quick validation:

```bash
python scripts/stress-test.py --smoke --mode all --concurrency 2
```

Raspberry Pi target runs:

```bash
python scripts/stress-test.py --mode large --large-bytes 5368709120
python scripts/stress-test.py --mode small --small-files 10000 --small-bytes 4096
python scripts/stress-test.py --mode small --small-files 10000 --small-bytes 4096 --concurrency 2
python scripts/stress-test.py --mode failure
```

Use a dedicated empty workspace on a large test filesystem when `/tmp` lacks capacity:

```bash
python scripts/stress-test.py --mode large \
  --temporary-root /path/to/dedicated-stress-workspace
```

The harness creates and removes uniquely named child directories. It never treats the
supplied workspace itself as an archive source or cleanup target.

The large case requires roughly six times the source size because source, active,
versioned backup, encrypted cold object, local restore, and remote restore coexist. A
many-file case approaches four times total source size plus four sample restore files.
The harness refuses to start when the temporary filesystem lacks estimated free space.

Failure mode interrupts:

- streaming download after 1 KiB;
- local backup after its object was written but before Mnema received success;
- cold upload after its encrypted object was written but before Mnema received success.

Each case closes the transaction, runs startup reconciliation, confirms
`FAILED_RETRYABLE`, explicitly queues the synthetic item, finishes the workflow, checks
for duplicate objects, and verifies local and remote restores. Ambiguous deletion recovery
is tested separately because this harness never invokes deletion.

Default large size is 5 GiB and default small-file count is 10,000. Compare
`source_larger_than_physical_memory` before claiming a larger-than-RAM result. The JSON
report includes elapsed time, peak process RSS, SQLite size, receipts, and object counts.

## Real Kopia and MinIO backend

Run the wrapper as root with a dedicated workspace on a filesystem with enough capacity:

```bash
sudo scripts/run-external-stress.sh /path/to/dedicated-workspace \
  --mode large --large-bytes 1073741824 --concurrency 1
sudo scripts/run-external-stress.sh /path/to/dedicated-workspace \
  --mode small --small-files 1000 --small-bytes 4096 --concurrency 1
```

The wrapper creates a private Docker network, dedicated MinIO container, random temporary
credentials, new Kopia repository, new bucket, and generated source data. It mounts no
Mnema configuration, production database, production MinIO data, or active NAS storage.
The internal Docker network has no external route. Cleanup removes the container, network,
credentials, repository, bucket data, and generated files. The supplied workspace itself
is never recursively removed.
