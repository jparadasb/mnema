from pathlib import Path

from mnema.domain.storage import StorageIdentity, storage_is_separate


def test_uuid_separation_has_priority() -> None:
    active = StorageIdentity(Path("/a"), 1, "uuid-a")
    backup = StorageIdentity(Path("/b"), 1, "uuid-b")
    assert storage_is_separate(active, backup)


def test_same_device_without_uuid_is_unsafe() -> None:
    active = StorageIdentity(Path("/a"), 7)
    backup = StorageIdentity(Path("/b"), 7)
    assert not storage_is_separate(active, backup)
