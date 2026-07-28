from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from pathlib import PurePosixPath

from mnema.config import SourcePolicy
from mnema.domain.source import SourceObject


@dataclass(frozen=True)
class PolicyDecision:
    eligible: bool
    reasons: tuple[str, ...]


def evaluate_policy(
    item: SourceObject,
    policy: SourcePolicy,
    *,
    now: datetime | None = None,
) -> PolicyDecision:
    now = now or datetime.now(UTC)
    path = PurePosixPath(item.relative_path)
    reasons: list[str] = []
    if policy.included_directories and path.parts[0] not in policy.included_directories:
        reasons.append("directory not included")
    if path.parts and path.parts[0] in policy.excluded_directories:
        reasons.append("directory excluded")
    if policy.included_globs and not any(
        fnmatch(item.relative_path, pattern) for pattern in policy.included_globs
    ):
        reasons.append("pattern not included")
    if any(fnmatch(item.relative_path, pattern) for pattern in policy.excluded_globs):
        reasons.append("pattern excluded")
    if item.size < policy.minimum_file_size:
        reasons.append("below minimum file size")
    if policy.maximum_file_size is not None and item.size > policy.maximum_file_size:
        reasons.append("above maximum file size")
    required_age = max(
        timedelta(days=policy.archive_after_days),
        timedelta(hours=policy.stability_window_hours),
    )
    if now - item.modified_at < required_age:
        reasons.append("age or stability window not reached")
    return PolicyDecision(not reasons, tuple(reasons))
