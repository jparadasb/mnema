from pathlib import Path

import pytest

from mnema.domain.storage import UnsafePath, resolve_beneath, safe_relative_path


@pytest.mark.parametrize("value", ["/etc/passwd", "../escape", "a/../../escape", "."])
def test_rejects_unsafe_relative_path(value: str) -> None:
    with pytest.raises(UnsafePath):
        safe_relative_path(value)


def test_resolve_rejects_symlink(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "target"
    target.write_text("secret")
    (root / "link").symlink_to(target)
    with pytest.raises(UnsafePath):
        resolve_beneath(root, "link", must_exist=True)


def test_resolve_accepts_regular_file(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    file = root / "safe.txt"
    file.write_text("safe")
    assert resolve_beneath(root, "safe.txt", must_exist=True) == file
