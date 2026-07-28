from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from mnema.domain.states import ArchiveState, validate_transition
from mnema.jobs.models import ArchiveItem, AuditEvent
from mnema.security.redaction import redact


def transition_item(
    session: Session,
    item: ArchiveItem,
    target: ArchiveState,
    *,
    actor: str,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    current = item.state
    validate_transition(current, target)
    item.state = target
    event = AuditEvent(
        archive_item_id=item.id,
        event_type="state_transition",
        from_state=current.value,
        to_state=target.value,
        actor=actor,
        details=redact(details or {}),
    )
    session.add(item)
    session.add(event)
    session.flush()
    return event
