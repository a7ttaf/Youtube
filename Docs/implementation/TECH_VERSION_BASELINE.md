# UMS Latest Stable Version Baseline

## Policy
Baseline verified: `2026-05-10T20:34:52Z` by Codex review agent.

Use latest stable/LTS runtime versions and current stable package releases at the time this baseline was checked. Do not use preview, alpha, beta, release-candidate, canary, or experimental releases for production foundations.

For this project:
- Runtime platforms use stable or LTS releases.
- Backend dependencies are pinned exactly in `pyproject.toml` until a lockfile workflow is introduced.
- Frontend versions are recorded as the target baseline, but no frontend app has been scaffolded yet.
- PostgreSQL remains the financial source of truth.
- Neo4j remains a read-only graph projection.

## Runtime and Platform Baseline

| Component | Version | Use |
|---|---:|---|
| Python | `3.14.5` | Backend runtime target |
| Node.js | `24.15.0` LTS | Future Next.js frontend runtime |
| PostgreSQL | `18.3` | Source-of-truth operational database |
| Neo4j Enterprise | `2026.04.0` | Read-only graph projection target |

## Backend Package Baseline

| Package | Version |
|---|---:|
| FastAPI | `0.136.1` |
| Pydantic | `2.13.4` |
| Uvicorn | `0.46.0` |
| SQLAlchemy | `2.0.49` |
| Alembic | `1.18.4` |
| asyncpg | `0.31.0` |
| neo4j Python driver | `6.2.0` |
| Celery | `5.6.3` |
| Redis Python client | `7.4.0` |
| openpyxl | `3.1.5` |
| ReportLab | `4.5.1` |
| python-pptx | `1.0.2` |
| pytest | `9.0.3` |
| httpx | `0.28.1` |
| pypdf | `6.11.0` |

## Frontend Target Baseline

| Package | Version |
|---|---:|
| Next.js | `16.2.6` |
| React | `19.2.6` |
| React DOM | `19.2.6` |
| TypeScript | `6.0.3` |
| ESLint | `10.3.0` |
| Vitest | `4.1.5` |
| Playwright | `1.59.1` |

## Checked Sources
Checked on: `2026-05-10T20:34:52Z` (UTC)

- Python downloads page: `https://www.python.org/downloads/`
- Node.js releases page: `https://nodejs.org/en/about/releases/`
- PostgreSQL home/releases page: `https://www.postgresql.org/`
- Neo4j supported versions page: `https://neo4j.com/developer/kb/neo4j-supported-versions/`
- PyPI JSON package metadata for backend packages: `https://pypi.org/pypi/<package>/json`
- npm registry latest package metadata for frontend packages: `https://registry.npmjs.org/<package>/latest`

Addendum checked on: `2026-05-13` (Africa/Cairo local date)

- PyPI openpyxl release metadata: `https://pypi.org/pypi/openpyxl`
- PyPI ReportLab release metadata: `https://pypi.org/pypi/reportlab`
- PyPI pypdf release metadata: `https://pypi.org/pypi/pypdf`
- PyPI python-pptx release metadata: `https://pypi.org/pypi/python-pptx`

## Local Environment Note
The current workstation Python is `3.12`, so tests are still run against the available local interpreter. The production runtime target is `3.14.5`, and the project metadata now expresses `>=3.14,<3.15`.
