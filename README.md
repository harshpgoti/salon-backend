# Salon backend (FastAPI)

Tenant-safe salon operations API. Synthetic local development only — no real
customer data, no live providers.

Origin: extracted from the `salon` workspace monorepo at `cabbad2`
(`apps/api` → repo root). Workspace plan state stays in the parent
`salon/.loop-engineer` directory.

## Layout

- `src/modules/` — feature modules (`identity`, `crm`, `catalogue`, `booking`,
  `communications`, …) plus `salon_platform/` tenant helpers.
- `tests/` — pytest integration suite, real PostgreSQL 17 required.
- `db/migrations/` — numbered SQL, applied in order (`000_` …). Mounted into
  the dev container; a fresh volume applies them automatically. Other
  environments apply new files by hand with `psql -f`, never by deleting data.
- `docker-compose.yml` — local PostgreSQL 17 on `127.0.0.1:5433`
  (compose project name `salon`, volume `salon-postgres` shared with history).

## Develop

```bash
python -m venv .venv
.venv/Scripts/python -m pip install --constraint constraints.txt -e ".[test]"
.venv/Scripts/python -m pip check
.venv/Scripts/python scripts/check_dependency_lock.py
docker compose up -d
set "DATABASE_URL=postgresql+psycopg://salon_app:local-dev-only@127.0.0.1:5433/salon"
.venv/Scripts/python -m pytest -q tests
```

## Conventions

- Every tenant-owned query is server-scoped (RLS + composite FKs); tests prove
  cross-tenant access fails.
- Mutations are idempotent; outbox rows are the integration seam for the worker.
- Facts are labelled: recorded salon receipts ≠ booked value ≠ SaaS revenue.
- No contact or message content in telemetry; synthetic fixtures only.

## Reproducible dependencies

constraints.txt pins the complete tested Python runtime/test graph. Docker wheel resolution and CI use it; unused test packages are excluded from the runtime image. The installed-package checker rejects unlisted distributions or version drift, and pip check verifies declared compatibility. Use a project-only virtual environment. To change dependencies, update the constraints deliberately, verify compatibility, then rebuild and inspect Docker; do not bypass the checker. Python and Node base manifests are digest-pinned; reviewed security updates require refreshing those digests and repeating checks. Installer pip and build-system setuptools remain separately pinned. Exact version constraints and image digests do not constitute an OS or application security audit.
