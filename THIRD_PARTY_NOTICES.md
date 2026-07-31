# Third-party notices

Declared direct dependencies:

- Alembic 1.18.5 — MIT
- AnyIO 4.9.0 — MIT
- boto3 1.39.14 and botocore — Apache-2.0
- cryptography 45.0.5 — Apache-2.0 OR BSD-3-Clause
- FastAPI 0.116.1 — MIT
- icloudpd 1.32.3 — MIT
- itsdangerous 2.2.0 — BSD-3-Clause
- Jinja2 3.1.6 — BSD-3-Clause
- Playwright 1.61.0 — Apache-2.0 (development and E2E only)
- Pydantic 2.11.7 and pydantic-settings 2.10.1 — MIT
- python-multipart 0.0.20 — Apache-2.0
- PyYAML 6.0.2 — MIT
- PyJWT 2.13.0 — MIT
- pyee 13.0.1 — MIT (Playwright transitive dependency)
- SQLAlchemy 2.0.41 — MIT
- Starlette 0.47.3 — BSD-3-Clause
- Typer 0.16.0 — MIT
- Uvicorn 0.35.0 — BSD-3-Clause

External service images/tools:

- SFTPGo 2.6.6 — AGPL-3.0
- MinIO Server RELEASE.2025-07-23T15-54-02Z — AGPL-3.0
- cloudflared 2025.7.0 — Apache-2.0
- Kopia — Apache-2.0
- rclone — MIT
- OpenMediaVault — GPL-3.0

Transitive dependencies retain their own notices and licenses. Generate exact installed report with:

```bash
pip-licenses --format=markdown --with-urls --with-license-file --output-file=build/THIRD_PARTY_INSTALLED.md
```

This inventory is engineering due diligence, not legal advice. Repeat review for exact release artifacts and container layers.
