from datetime import UTC, datetime, timedelta

from mnema.jobs import Database, DurableQueue
from mnema.jobs.models import JobStatus


def test_idempotent_enqueue_and_lease_recovery() -> None:
    database = Database("sqlite://")
    database.create_schema()
    queue = DurableQueue()
    with database.session() as session:
        first = queue.enqueue(
            session,
            kind="archive",
            adapter="local",
            payload={"id": 1},
            idempotency_key="same",
        )
        second = queue.enqueue(
            session,
            kind="archive",
            adapter="local",
            payload={"id": 1},
            idempotency_key="same",
        )
        assert first.id == second.id
        leased = queue.lease(session, worker_id="w1", adapter="local")
        assert leased is not None and leased.status == JobStatus.RUNNING
        leased.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with database.session() as session:
        assert queue.recover_expired(session) == 1
        recovered = queue.lease(session, worker_id="w2", adapter="local")
        assert recovered is not None
        assert recovered.attempts == 2


def test_retry_backoff_and_dead_letter() -> None:
    database = Database("sqlite://")
    database.create_schema()
    queue = DurableQueue()
    with database.session() as session:
        job = queue.enqueue(
            session,
            kind="archive",
            adapter="local",
            payload={},
            idempotency_key="failure",
            max_attempts=1,
        )
        leased = queue.lease(session, worker_id="w", adapter="local")
        assert leased is job
        queue.fail(session, job, "w", "password=secret")
        assert job.status == JobStatus.MANUAL_REVIEW
