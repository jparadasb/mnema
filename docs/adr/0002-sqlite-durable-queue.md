# ADR 0002: SQLite metadata and durable queue

Status: accepted

Use SQLAlchemy 2 with SQLite WAL, foreign keys, busy timeout, and a lease-based queue. One worker is default; maximum concurrency is two.

Reason: single-appliance workload does not justify Redis, Celery, or PostgreSQL.

Consequence: write throughput is bounded; horizontally distributed workers are unsupported.

