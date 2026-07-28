# ADR 0001: Reuse mature storage services

Status: accepted

Mnema orchestrates OpenMediaVault, SFTPGo, Kopia, rclone/object APIs, and optional Cloudflare Tunnel. It does not implement SMB, SFTP, WebDAV, file browsing, backup formats, OAuth, or NAS administration.

Reason: smaller attack surface and maintenance burden; established restore/protocol behavior.

Consequence: availability and license/update posture of separate services are operational dependencies.

