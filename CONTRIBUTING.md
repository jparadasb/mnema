# Contributing

Use synthetic data. Never add real cloud credentials or personal files.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
pre-commit install
ruff check .
ruff format --check .
mypy src
pytest
```

Changes affecting state transitions, deletion gates, cryptography, path handling, authentication, installer privileges, mounts, or backup verification require tests and an ADR/security review. New source adapters must demonstrate stable identity, mutation detection, idempotency, interrupted-transfer recovery, and ambiguous-delete handling.

Use conventional, focused commits. Do not weaken guards to make a test pass.

