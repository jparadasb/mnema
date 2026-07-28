from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, select, update
from sqlalchemy.orm import Session

from mnema.jobs.models import Job, JobLog, JobStatus, WorkerHeartbeat
from mnema.security.redaction import redact


def _now() -> datetime:
    return datetime.now(UTC)


class DurableQueue:
    def enqueue(
        self,
        session: Session,
        *,
        kind: str,
        adapter: str,
        payload: dict[str, Any],
        idempotency_key: str,
        max_attempts: int = 5,
    ) -> Job:
        existing = session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if existing is not None:
            return existing
        job = Job(
            kind=kind,
            adapter=adapter,
            payload=payload,
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
        )
        session.add(job)
        session.flush()
        return job

    def lease(
        self,
        session: Session,
        *,
        worker_id: str,
        adapter: str,
        lease_seconds: int = 60,
        adapter_limit: int = 1,
        global_limit: int = 1,
    ) -> Job | None:
        now = _now()
        running_global = session.scalars(
            select(Job).where(Job.status == JobStatus.RUNNING, Job.lease_expires_at > now)
        ).all()
        if len(running_global) >= global_limit:
            return None
        if sum(job.adapter == adapter for job in running_global) >= adapter_limit:
            return None
        query: Select[tuple[Job]] = (
            select(Job)
            .where(
                Job.adapter == adapter,
                Job.status.in_([JobStatus.PENDING, JobStatus.RETRY]),
                Job.available_at <= now,
            )
            .order_by(Job.available_at, Job.id)
            .limit(1)
        )
        candidate = session.scalar(query)
        if candidate is None:
            return None
        claimed = session.execute(
            update(Job)
            .where(
                Job.id == candidate.id,
                Job.status.in_([JobStatus.PENDING, JobStatus.RETRY]),
            )
            .values(
                status=JobStatus.RUNNING,
                lease_owner=worker_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                heartbeat_at=now,
                attempts=Job.attempts + 1,
            )
        )
        if claimed.rowcount != 1:
            return None
        session.flush()
        session.refresh(candidate)
        return candidate

    def heartbeat(
        self,
        session: Session,
        job: Job,
        worker_id: str,
        *,
        lease_seconds: int = 60,
    ) -> None:
        if job.status != JobStatus.RUNNING or job.lease_owner != worker_id:
            raise RuntimeError("worker does not own job lease")
        now = _now()
        job.heartbeat_at = now
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        heartbeat = session.get(WorkerHeartbeat, worker_id)
        if heartbeat is None:
            heartbeat = WorkerHeartbeat(worker_id=worker_id, adapter=job.adapter)
            session.add(heartbeat)
        heartbeat.heartbeat_at = now

    def succeed(self, session: Session, job: Job, worker_id: str) -> None:
        self._require_owner(job, worker_id)
        job.status = JobStatus.SUCCEEDED
        job.lease_owner = None
        job.lease_expires_at = None

    def fail(
        self,
        session: Session,
        job: Job,
        worker_id: str,
        error: str,
        *,
        base_backoff_seconds: int = 5,
    ) -> None:
        self._require_owner(job, worker_id)
        job.last_error = str(redact(error))
        job.lease_owner = None
        job.lease_expires_at = None
        if job.attempts >= job.max_attempts:
            job.status = JobStatus.MANUAL_REVIEW
        else:
            job.status = JobStatus.RETRY
            exponent = min(job.attempts - 1, 10)
            job.available_at = _now() + timedelta(
                seconds=math.pow(2, exponent) * base_backoff_seconds
            )

    def recover_expired(self, session: Session, *, now: datetime | None = None) -> int:
        now = now or _now()
        result = session.execute(
            update(Job)
            .where(Job.status == JobStatus.RUNNING, Job.lease_expires_at < now)
            .values(
                status=JobStatus.RETRY,
                lease_owner=None,
                lease_expires_at=None,
                available_at=now,
                last_error="lease expired; recovered after interruption",
            )
        )
        return int(result.rowcount)

    def log(
        self,
        session: Session,
        job: Job,
        level: str,
        message: str,
        **fields: Any,
    ) -> None:
        session.add(
            JobLog(
                job_id=job.id,
                level=level,
                message=str(redact(message)),
                fields=redact(fields),
            )
        )

    @staticmethod
    def _require_owner(job: Job, worker_id: str) -> None:
        if job.status != JobStatus.RUNNING or job.lease_owner != worker_id:
            raise RuntimeError("worker does not own job lease")
