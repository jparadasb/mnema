# ADR 0003: Fail-closed deletion gate

Status: accepted

Production deletion is absent. Test deletion is disabled by default and requires every independently observed prerequisite, immediate source revalidation, explicit environment test flag, and per-run limits.

Reason: deletion is irreversible and uncertain external results cannot be inferred from process exit.

Consequence: false negatives and manual review are preferred over unsafe progress.

