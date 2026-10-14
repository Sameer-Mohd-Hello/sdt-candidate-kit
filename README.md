# HROne Employee Attendance & Analytics API

This project implements the FastAPI employee attendance and analytics service described by the assignment contract in `openapi.yaml` and the MongoDB data model in `DATA_MODEL.md`.

## What the service does
- Create and list employees
- Record punch-in and punch-out events
- Maintain attendance records with audit history for manual corrections
- Calculate late minutes, work hours, overtime, and half-day flags according to the assignment rules
- Produce monthly, department, and leaderboard analytics
- Expose a readiness endpoint for deployment checks

## Project structure
- `app/main.py` – FastAPI app and business logic
- `sample_data/` – sample MongoDB JSON documents used for local testing
- `sample_seed.py` – loads the seed data into MongoDB
- `openapi.yaml` – API contract and business rules
- `DATA_MODEL.md` – MongoDB schema and stored fields
- `REVIEW.md` – defect review for the starter implementation
- `DECISIONS.md` – design decisions and trade-offs

## Requirements
- Python 3.11+
- MongoDB 6.0 or newer
- Environment variables:
  - `MONGO_URI` (default: `mongodb://localhost:27017`)
  - `MONGO_DB` (default: `attendance_db`)

## Local setup
```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# macOS/Linux
# source .venv/bin/activate

pip install -r requirements.txt
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

If you need a local MongoDB instance:
```bash
docker run -p 27017:27017 --name mongo -d mongo:7
```

## Health check
The service is expected to run with:
```bash
uvicorn app.main:app --port 8000
```

The readiness probe is available at:
```http
GET /health
```

## Notes
- The application keeps MongoDB indexes idempotent at startup.
- The API expects ISO date values in `YYYY-MM-DD` format and epoch-millisecond timestamps for instants.
- No secrets or environment files are committed to this project.
