from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlencode


class ICloudControlError(RuntimeError):
    """Apple operation was unavailable or could not be independently confirmed."""


@dataclass(frozen=True)
class ICloudQuota:
    used_bytes: int
    quota_bytes: int

    @property
    def used_percent(self) -> float:
        return self.used_bytes / self.quota_bytes * 100


@dataclass(frozen=True)
class ICloudRemoteAsset:
    apple_asset_id: str
    asset_record_name: str
    change_tag: str
    created_at: datetime
    size_bytes: int
    favorite: bool
    expected_components: int


class ICloudControlClient(Protocol):
    def quota(self) -> ICloudQuota: ...

    def assets(self) -> tuple[ICloudRemoteAsset, ...]: ...

    def delete_to_recently_deleted(
        self, apple_asset_id: str, asset_record_name: str, change_tag: str
    ) -> bool: ...


class PyiCloudControlClient:
    """Narrow wrapper over pinned icloudpd source. Never permanently deletes assets."""

    def __init__(self, apple_id: str, session_directory: Path) -> None:
        self.apple_id = apple_id
        self.session_directory = session_directory

    def _service(self) -> Any:
        try:
            module = import_module("pyicloud_ipd.base")
            service = module.PyiCloudService(
                "com",
                self.apple_id,
                lambda: None,
                cookie_directory=str(self.session_directory),
            )
        except Exception as error:
            raise ICloudControlError("iCloud authentication is unavailable") from error
        if not getattr(service, "data", None):
            raise ICloudControlError("iCloud authentication is unavailable")
        return service

    def quota(self) -> ICloudQuota:
        service = self._service()
        try:
            response = service.session.post(
                f"{service.SETUP_ENDPOINT}/storageUsageInfo", params=service.params
            )
            response.raise_for_status()
            payload = response.json()
            info = payload.get("storageUsageInfo", payload)
            quota = self._positive_int(info, "storageQuota", "quotaStorage", "quota")
            try:
                used = self._positive_int(info, "storageUsed", "totalStorageUsed", "used")
            except ValueError:
                media = info.get("storageUsageByMedia")
                if not isinstance(media, list):
                    raise
                usages: list[int] = []
                for entry in media:
                    if not isinstance(entry, dict):
                        raise ValueError("iCloud media usage is malformed") from None
                    value = entry.get("usage")
                    if not isinstance(value, int | float):
                        raise ValueError("iCloud media usage is malformed") from None
                    usages.append(int(value))
                if not usages:
                    raise ValueError("iCloud media usage is malformed") from None
                used = sum(usages)
        except Exception as error:
            raise ICloudControlError("iCloud quota response is unavailable or malformed") from error
        if used > quota:
            raise ICloudControlError("iCloud quota response is inconsistent")
        return ICloudQuota(used, quota)

    @staticmethod
    def _positive_int(payload: dict[str, Any], *keys: str) -> int:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, dict):
                value = value.get("value")
            if isinstance(value, int | float) and value >= 0:
                result = int(value)
                if key.lower().find("quota") >= 0 and result == 0:
                    break
                return result
        raise ValueError("required storage field is absent")

    def assets(self) -> tuple[ICloudRemoteAsset, ...]:
        service = self._service()
        try:
            return tuple(self._remote_asset(photo) for photo in service.photos)
        except Exception as error:
            raise ICloudControlError("iCloud asset inventory is unavailable") from error

    @staticmethod
    def _remote_asset(photo: Any) -> ICloudRemoteAsset:
        asset_record = cast(dict[str, Any], photo._asset_record)
        fields = asset_record["fields"]
        versions = photo.versions
        expected = 1 + int(any(type(key).__name__ == "LivePhotoVersionSize" for key in versions))
        size = int(photo.size)
        for key, version in versions.items():
            if (
                type(key).__name__ == "LivePhotoVersionSize"
                and getattr(key, "value", "") == "original"
            ):
                size += int(version.size)
                break
        favorite = bool(fields.get("isFavorite", {}).get("value", False))
        return ICloudRemoteAsset(
            apple_asset_id=str(photo.id),
            asset_record_name=str(asset_record["recordName"]),
            change_tag=str(asset_record["recordChangeTag"]),
            created_at=photo.asset_date,
            size_bytes=size,
            favorite=favorite,
            expected_components=expected,
        )

    def delete_to_recently_deleted(
        self, apple_asset_id: str, asset_record_name: str, change_tag: str
    ) -> bool:
        service = self._service()
        library = service.photos
        current = self._find(library, apple_asset_id)
        if current is None:
            return self._find(library.recently_deleted, apple_asset_id) is not None
        current_record = current._asset_record
        if (
            str(current_record["recordName"]) != asset_record_name
            or str(current_record["recordChangeTag"]) != change_tag
        ):
            raise ICloudControlError("iCloud asset changed after approval")
        url = f"{library.service_endpoint}/records/modify?{urlencode(library.params)}"
        body = {
            "atomic": True,
            "desiredKeys": ["isDeleted"],
            "operations": [
                {
                    "operationType": "update",
                    "record": {
                        "fields": {"isDeleted": {"value": 1}},
                        "recordChangeTag": change_tag,
                        "recordName": asset_record_name,
                        "recordType": "CPLAsset",
                    },
                }
            ],
            "zoneID": library.zone_id,
        }
        try:
            response = library.session.post(
                url, data=json.dumps(body), headers={"Content-type": "application/json"}
            )
            response.raise_for_status()
            payload = response.json()
            records = payload.get("records", [])
            if not records or any(record.get("serverErrorCode") for record in records):
                raise ICloudControlError("Apple rejected iCloud asset deletion")
            fresh = self._service().photos
            absent = self._find(fresh, apple_asset_id) is None
            in_recently_deleted = self._find(fresh.recently_deleted, apple_asset_id) is not None
            return absent and in_recently_deleted
        except ICloudControlError:
            raise
        except Exception as error:
            raise ICloudControlError("iCloud deletion result is ambiguous") from error

    @staticmethod
    def _find(album: Any, apple_asset_id: str) -> Any | None:
        for photo in album:
            if str(photo.id) == apple_asset_id:
                return photo
        return None
