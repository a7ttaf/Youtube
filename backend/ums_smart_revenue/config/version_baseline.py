STACK_VERSION_BASELINE: dict[str, dict[str, str]] = {
    "runtime": {
        "python": "3.14.5",
        "node_lts": "24.15.0",
    },
    "backend": {
        "fastapi": "0.136.1",
        "pydantic": "2.13.4",
        "uvicorn": "0.46.0",
        "sqlalchemy": "2.0.49",
        "alembic": "1.18.4",
        "psycopg": "3.3.4",
        "celery": "5.6.3",
        "redis": "7.4.0",
        "openpyxl": "3.1.5",
        "reportlab": "4.5.1",
        "python_pptx": "1.0.2",
        "pytest": "9.0.3",
        "httpx": "0.28.1",
        "pypdf": "6.11.0",
    },
    "datastores": {
        "postgresql": "18.3",
    },
    "frontend": {
        "next": "16.2.6",
        "react": "19.2.6",
        "react_dom": "19.2.6",
        "typescript": "6.0.3",
        "eslint": "10.3.0",
        "vitest": "4.1.5",
        "playwright": "1.59.1",
    },
}
