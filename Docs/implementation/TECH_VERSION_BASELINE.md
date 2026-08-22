# UMS Latest Stable Version Baseline

## Policy
Baseline verified: `2026-05-10T20:34:52Z` by Codex review agent.

Use latest stable/LTS runtime versions and current stable package releases at the time this baseline was checked. Do not use preview, alpha, beta, release-candidate, canary, or experimental releases for production foundations.

For this project:
- Runtime platforms use stable or LTS releases.
- Backend dependencies are pinned exactly in `pyproject.toml` until a lockfile workflow is introduced.
- A Vite preview frontend app has been scaffolded and verified with React; its runtime floor is Node.js `>=22.12.0`.
- PostgreSQL remains the financial source of truth.
- Neo4j is not part of the active architecture.

## Runtime and Platform Baseline

| Component | Version | Use |
|---|---:|---|
| Python | `3.14.5` | Backend runtime target |
| Node.js | `24.15.0` LTS | Future Next.js frontend runtime |
| PostgreSQL | `18.3` | Source-of-truth operational database |

## Backend Package Baseline

| Package | Version |
|---|---:|
| FastAPI | `0.136.1` |
| Pydantic | `2.13.4` |
| Uvicorn | `0.47.0` |
| SQLAlchemy | `2.0.49` |
| Alembic | `1.18.4` |
| psycopg | `3.3.4` |
| Celery | `5.6.3` |
| Redis Python client | `7.4.0` |
| openpyxl | `3.1.5` |
| ReportLab | `4.5.1` |
| python-pptx | `1.0.2` |
| pytest | `9.0.3` |
| httpx2 | `2.12.0` |
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
- PyPI JSON package metadata for backend packages: `https://pypi.org/pypi/<package>/json`
- npm registry latest package metadata for frontend packages: `https://registry.npmjs.org/<package>/latest`

Frontend design addendum checked on: `2026-05-13` (Africa/Cairo local date)

- npm registry latest package metadata verified for the new Vite preview app:
  React `19.2.6`, React DOM `19.2.6`, Vite `8.0.12`,
  `@vitejs/plugin-react` `6.0.1`, Tailwind CSS `4.3.0`,
  `@tailwindcss/vite` `4.3.0`, `tw-animate-css` `1.4.0`, and
  TypeScript `6.0.3`.
- The frontend preview uses Vite because the approved design source is a Vite
  application. The runtime floor is Node.js `>=22.12.0`; the production target
  remains Node.js 24 LTS.

Backend package addendum checked on: `2026-05-13` (Africa/Cairo local date)

- PyPI openpyxl release metadata: `https://pypi.org/pypi/openpyxl`
- PyPI ReportLab release metadata: `https://pypi.org/pypi/reportlab`
- PyPI pypdf release metadata: `https://pypi.org/pypi/pypdf`
- PyPI python-pptx release metadata: `https://pypi.org/pypi/python-pptx`

## Local Environment Note
The current workstation Python is `3.12`, so tests are still run against the available local interpreter. The production runtime target is `3.14.5`, and the project metadata now expresses `>=3.14,<3.15`.
