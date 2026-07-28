import pytest

from mnema.domain.states import ArchiveState, InvalidTransition, validate_transition


def test_valid_linear_transition() -> None:
    validate_transition(ArchiveState.DOWNLOADING, ArchiveState.LOCAL_STAGED)


def test_invalid_transition() -> None:
    with pytest.raises(InvalidTransition):
        validate_transition(ArchiveState.DISCOVERED, ArchiveState.ARCHIVED)


def test_failure_and_same_state_are_allowed() -> None:
    validate_transition(ArchiveState.DOWNLOADING, ArchiveState.FAILED_RETRYABLE)
    validate_transition(ArchiveState.QUARANTINED, ArchiveState.QUARANTINED)
