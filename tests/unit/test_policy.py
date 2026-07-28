from datetime import UTC, datetime, timedelta

from mnema.config import SourcePolicy
from mnema.domain.source import SourceObject
from mnema.policies import evaluate_policy


def object_at(age: timedelta, path: str = "docs/file.txt", size: int = 100) -> SourceObject:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return SourceObject("id", path, size, now - age, "v1")


def test_age_and_stability_eligibility() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    policy = SourcePolicy(archive_after_days=30, stability_window_hours=24)
    assert evaluate_policy(object_at(timedelta(days=31)), policy, now=now).eligible
    assert not evaluate_policy(object_at(timedelta(days=2)), policy, now=now).eligible


def test_glob_directory_and_size_filters() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    policy = SourcePolicy(
        included_directories=("docs",),
        excluded_globs=("**/*.tmp",),
        minimum_file_size=10,
        maximum_file_size=1000,
        archive_after_days=0,
        stability_window_hours=0,
    )
    assert evaluate_policy(object_at(timedelta(days=1)), policy, now=now).eligible
    assert not evaluate_policy(
        object_at(timedelta(days=1), "docs/file.tmp"), policy, now=now
    ).eligible
