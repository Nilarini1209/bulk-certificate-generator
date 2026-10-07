# Bulk Certificate Generator

A backend API (FastAPI + SQLAlchemy + SQLite/any SQL database + ReportLab) that accepts a
list of recipients in **one request**, generates a PDF certificate for each valid recipient
from a single predefined template, tracks progress, and lets the client download results.

## Setup

Requires Python 3.10+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt      # use requirements.txt for runtime only
```

## Run

```bash
uvicorn app.main:create_app --factory --reload
```

Interactive docs: http://127.0.0.1:8000/docs

Configuration (environment variables):

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./certgen.db` | Any SQLAlchemy URL, e.g. `postgresql+psycopg://user:pw@host/db` |
| `STORAGE_DIR` | `./storage` | Where generated PDFs are written |
| `MAX_RECIPIENTS` | `5000` | Max recipients per request |

## Test

```bash
pytest
```

## API

### 1. Submit a job - `POST /api/v1/jobs` (returns `202 Accepted`)

```bash
curl -X POST http://127.0.0.1:8000/api/v1/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "course_name": "Python Data Science Workshop",
    "title": "Certificate of Completion",
    "issuer": "Acme Academy",
    "issued_on": "2026-10-01",
    "recipients": [
      {"name": "Nilarini Devaraj", "email": "nilarini@example.com"},
      {"name": "Alan Turing"},
      {"name": "", "email": "broken@example.com"}
    ]
  }'
```

`title`, `issuer`, `issued_on` (defaults to today) are optional. Recipient `email` is optional.

### 2. Check progress - `GET /api/v1/jobs/{job_id}`

```json
{"id": "...", "status": "completed_with_errors", "total": 3, "succeeded": 2,
 "failed": 1, "pending": 0, "progress_percent": 100.0, "links": {...}}
```

Job status: `pending` -> `processing` -> `completed` (all ok) | `completed_with_errors`
(some failed) | `failed` (none succeeded).

### 3. See per-recipient results - `GET /api/v1/jobs/{job_id}/certificates`

Query params: `status=success|failed|pending`, `limit` (1-1000, default 100), `offset`.
Each item has `row_index` (position in your submitted list), `status`, `error`
(for failures) and `download_url` (for successes). To list only failures:
`.../certificates?status=failed`.

### 4. Retrieve certificates

* One PDF: `GET /api/v1/certificates/{certificate_id}/download`
* Everything in a job as a ZIP of successful PDFs: `GET /api/v1/jobs/{job_id}/download`

```bash
curl -OJ http://127.0.0.1:8000/api/v1/jobs/<job_id>/download
```

### Error codes

`422` invalid request shape / too many recipients / bad query param, `404` unknown job or
certificate, `409` certificate not available (failed or still pending) / nothing to zip yet,
`410` file missing from storage.

## Design decisions

**Processing model: asynchronous background processing.** `POST` validates, saves the job
and one row per recipient, returns `202` immediately, and a background task generates the
PDFs. Generating thousands of PDFs inside the request would hit client/proxy timeouts and
tie up a worker. The client polls the job endpoint. I used FastAPI `BackgroundTasks`
instead of Celery/RQ because it needs no extra infrastructure (broker, worker process) and
is deterministic under test. The trade-off is that work runs in the web process; to
mitigate that, progress is committed after every certificate and **on startup the app
resumes any job still `pending`/`processing`**, doing only the certificates still pending.
For multi-instance deployments, `services.process_job(session_factory, storage_dir, job_id)`
is a plain function that can be moved into a Celery/RQ task unchanged.

**Two-level validation.**
1. *Request level* (Pydantic -> `422`, nothing saved): missing `course_name`, empty
   recipient list, malformed date, more than `MAX_RECIPIENTS`.
2. *Recipient level*: each recipient is validated individually (name required, 1-120
   chars, optional email must look valid, duplicate emails in the same job rejected). Invalid
   ones are stored as `failed` with a human-readable `error` and the rest of the job proceeds.
   `recipients` is deliberately `list[Any]` in the request schema so one malformed item can't
   cause the whole request to be rejected.

**Failure isolation.** Each certificate is generated inside its own `try/except` and
committed on its own. Any exception (renderer, disk) marks only that certificate failed.

**Every input row is tracked.** One `certificates` row per submitted recipient, including
invalid ones, with its `row_index`, so a client can reconcile results with its input.

**Counts are derived, not stored.** Job totals come from `GROUP BY status` on the
certificates table, so they can never drift out of sync with the actual rows.

**Storage.** PDFs are written to disk (`STORAGE_DIR/<job_id>/<certificate_id>.pdf`) using
write-to-temp + atomic rename; the DB stores only the path. Swapping to S3 would only touch
`services._write_atomic` and the download endpoints.

**Database.** SQLite by default for zero-setup; set `DATABASE_URL` for PostgreSQL/MySQL.
Tables are created at startup (`create_all`); a real deployment would use Alembic.

## Project layout

```
app/
  main.py      # app factory + HTTP endpoints
  services.py  # job creation, validation, background processing, queries
  pdf.py       # the single certificate template (ReportLab)
  models.py    # Job, Certificate tables
  schemas.py   # request/response models
  config.py, database.py
tests/         # 21 tests
```

## Known limitations / possible next steps

* Built-in PDF fonts are Latin-only; names in other scripts would fail to render (and
  appear as failed certificates with an explanatory error). Fix: register a Unicode TTF font.
* No authentication, rate limiting or idempotency keys.
* The ZIP is built in memory; for very large jobs stream it to a temp file.
* A Celery/RQ worker (see above) for horizontal scaling.
* SQLite drops timezone info on read, so `created_at` may appear without a `Z` suffix; use
  PostgreSQL in production.
