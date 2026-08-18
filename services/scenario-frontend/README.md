# scenario-frontend

React + Vite + TypeScript web app for RAGAS Evaluation. Lets you upload a golden-dataset spreadsheet, tag it with a domain (HR / Contact Center), review the "Test Scenario" / "Expected Result" columns in an editable table, and save edits back to `services/scenario-db-service`.

Talks only to `scenario-db-service`'s HTTP API — no direct database or file access, and no dependency on `services/ragas-service`.

## Run it

Backend must be running first (see `services/scenario-db-service/README.md`).

**Local dev (hot reload):**

```bash
cd services/scenario-frontend
npm install
cp .env.example .env   # points at the backend; edit VITE_API_BASE_URL if it's not on localhost:8000
npm run dev
```

Open `http://localhost:5173`.

**Docker:**

```bash
cd services/scenario-frontend
docker compose up --build
```

Serves the production build via nginx at `http://localhost:5173`. `VITE_API_BASE_URL` is baked into the JS bundle at build time (see `Dockerfile` `ARG`/`ENV`), so set it before building if the backend isn't at `http://localhost:8000`.

## What it does

1. Pick a domain (HR / Contact Center) and upload a `.xlsx` file → `POST /api/datasets`.
2. The uploaded rows render in an editable table (Test Scenario / Expected Result columns).
3. Edit any cell, then **Save Changes** → `PUT /api/datasets/{id}/rows` persists the edits.
4. The dataset picker lets you switch between previously uploaded files for the selected domain.
