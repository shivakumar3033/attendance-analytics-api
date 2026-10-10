# Employee Attendance & Analytics API

FastAPI + MongoDB implementation for the HROne Engineering Assignment, contract v2.0.

## Run locally

Requirements: Python 3.11+ and MongoDB 6.0+.

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` in your shell or in a local `.env` file. Real process environment variables take priority. Do not commit `.env`.

```bash
uvicorn app.main:app --port 8000
```

Readiness check: `GET http://127.0.0.1:8000/health`. Interactive API docs: `http://127.0.0.1:8000/docs`.

## Implementation notes

- `app/main.py` contains the application and all endpoint implementations.
- Indexes are created idempotently at startup. The unique `(emp_code, date)` attendance index enforces race-safe duplicate prevention.
- Analytics are implemented with MongoDB aggregation pipelines; the trend builds its calendar series in MongoDB.
- `REVIEW.md` records the defects found in the supplied starter; `DECISIONS.md` explains the design choices.
- The assignment's original `openapi.yaml` and `DATA_MODEL.md` are used as the implementation contract but are not needed in the submission repository unless the recruiter explicitly requests them.

Before submission, run against MongoDB with the provided seed data and test concurrency and edge cases. The code was syntax-checked, but the hidden 100,000-record evaluation dataset and MongoDB execution plans are not available here; do not claim hidden-test or performance validation without running those checks.
