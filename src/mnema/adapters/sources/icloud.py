from __future__ import annotations


class ICloudDriveSourceAdapter:
    """Future boundary. No authentication, transfer, or deletion is implemented."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise NotImplementedError("iCloud Drive support is not implemented")


class ICloudPhotosSourceAdapter:
    """Future boundary. Photos capabilities differ from iCloud Drive."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise NotImplementedError("iCloud Photos support is not implemented")
