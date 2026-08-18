# scenario-db-service

Backend API + PostgreSQL database for the Scenario Manager web app. Stores golden-dataset scenarios (Test Scenario / Expected Result, tagged by domain, built via upload or typed manually) so they can be reviewed and edited from `services/scenario-frontend`, and can trigger a real RAGAS evaluation run against the live AI Platform as the logged-in user.

Independent of `services/ragas-service` — doesn't call or import that tool. Domain values (`hr` / `contact_center`) match `SHEET_TO_DOMAIN` in `ragas_evaluation.py`, and `app/ragas_runner.py`'s judge/scoring setup is adapted from that script's `_setup_judge`/`_score_one_metric`, but nothing wires the two services together automatically.

## Run it

```bash
cd services/scenario-db-service
cp .env.example .env
# fill in AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (same values as services/ragas-service/.env)
docker compose up --build
```

- API: `http://localhost:8000` (interactive docs at `/docs`)
- Postgres: host port `5433` (avoids clashing with a local default-`5432` Postgres)
- **Adminer** (web DB browser — see "Seeing your data" below): `http://localhost:8080`

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/auth/login` | `{email, password}` → relays to the real AI Platform's login; returns `{access_token, user_id, email, roles}`. Password is never stored. |
| `POST` | `/api/datasets` | Upload a `.xlsx` (multipart form: `file`, `domain`). Parses and stores the rows. |
| `POST` | `/api/datasets/manual` | `{domain, name?}` → creates an empty dataset for typing rows by hand. |
| `GET` | `/api/datasets?domain=hr` | List datasets for a domain, newest first. |
| `GET` | `/api/datasets/{id}` | Full dataset with all rows. |
| `POST` | `/api/datasets/{id}/rows` | Append one new row: `{test_scenario, expected_result}`. |
| `PUT` | `/api/datasets/{id}/rows` | Bulk-update existing row text: `[{id, test_scenario, expected_result}, ...]`. |
| `DELETE` | `/api/datasets/{id}/rows/{row_id}` | Remove a single row. |
| `DELETE` | `/api/datasets/{id}` | Delete a dataset and its rows. |
| `POST` | `/api/runs` | `{dataset_id}` + `Authorization: Bearer <access_token>` → starts a background RAGAS run as that user, returns the run (status `pending`). |
| `GET` | `/api/runs/{id}` | Run status + summary (per-metric averages) once `completed`. |
| `GET` | `/api/runs/{id}/results` | Per-row scores, answers, and contexts for a run. |
| `GET` | `/api/runs?dataset_id=` | Run history for a dataset. |

`domain` must be `hr` or `contact_center`.

### Excel format (for `POST /api/datasets`)

The uploaded workbook needs a sheet with **"Test Scenario"** and **"Expected Result"** columns (aliases `Query` / `Expected Answer` / `Ground Truth` also accepted, case-insensitive). If the workbook has a sheet literally named `HR` or `Contact Center`, the one matching the selected domain is read; otherwise the first sheet is used. Rows with an empty Test Scenario are dropped.

### How a run works

`POST /api/runs` re-validates the bearer token against the AI Platform (`GET /api/v1/auth/me`) to get the current user/roles, then schedules `app.ragas_runner.run_evaluation` as a FastAPI `BackgroundTasks` job and returns immediately. That job calls the platform's `/api/v1/chat` for every row in the dataset (as the logged-in user), then scores 5 core metrics — faithfulness, answer_relevancy, context_precision, context_recall, answer_correctness — each in its own isolated `ragas.evaluate()` call (same pattern as `ragas_evaluation.py`, so one metric failing doesn't wipe out the others), using a Bedrock Nova Pro judge + SageMaker bge-m3 embeddings. Poll `GET /api/runs/{id}` until `status` is `completed` or `failed`.

This gives per-row scores + averages for the web UI — not the full trace/Excel/comparison-history workbook `ragas_evaluation.py` produces. Use the CLI tool if you need that deeper diagnostic trace.

## Data model

- `scenario_datasets` / `scenario_rows` — as before (dataset = one upload or manual set; rows = its scenarios).
- `evaluation_runs` — one row per RAGAS run (`dataset_id`, `domain`, `requested_by_email`, `status`, `summary` JSON, timestamps).
- `evaluation_run_results` — one row per scenario per run (`question`, `ground_truth`, `answer`, `contexts`, `metrics` JSON, `latency_seconds`).

Tables are created automatically on startup (`Base.metadata.create_all`) — no migration tooling for this small a schema.

## Seeing your data (no SQL required)

This service's database is plain PostgreSQL — `docker compose up` also starts **Adminer**, a lightweight web UI for browsing it:

1. Open `http://localhost:8080`
2. System: **PostgreSQL**, Server: `db`, Username: `scenario`, Password: whatever's in your `.env` (`POSTGRES_PASSWORD`), Database: `scenario_db`
3. Browse `scenario_datasets`, `scenario_rows`, `evaluation_runs`, `evaluation_run_results` — click any table to see its schema and rows, or use the built-in SQL query box.

## Local dev without Docker

```bash
python -m venv .venv && .venv\Scripts\activate  # Windows
pip install -r requirements.txt
$env:DATABASE_URL = "postgresql+psycopg://scenario:scenario@localhost:5433/scenario_db"
uvicorn app.main:app --reload
```

## Suggestions if you're newer to backend work

- **Adminer** is the fastest way to *see* what's actually in the database — no SQL needed to start, though the query box is there once you're ready.
- Runs execute via FastAPI's `BackgroundTasks` — simplest "do this after responding" mechanism available. It runs in-process, so a run in progress won't survive an API restart. If this ever needs to run many jobs reliably/concurrently or survive restarts, that's when to look at a real task queue (Celery, RQ, Dramatiq).
- The AI Platform access token is only ever held in memory (passed through the login response, kept by the frontend, sent back on `POST /api/runs`) — it's never written to this database.
