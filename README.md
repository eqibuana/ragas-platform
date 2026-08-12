# ragas-platform

RAGAS-based quality evaluation for PermataBank AI Platform's RAG pipelines (People Care / HR agent and Contact Center agent). It drives the deployed chat API with a golden question set, then scores every answer with an LLM judge across faithfulness, relevancy, context precision/recall, correctness, noise sensitivity, and summarization quality.

## Repository layout

```
ragas-platform/
  services/
    ragas-service/     # the evaluation tool — see its own README for full details
      ragas_evaluation.py
      ragas_data.xlsx  # golden dataset (HR + Contact Center sheets)
      Dockerfile
      docker-compose.yml
      output/          # generated results + run logs (gitignored)
  scripts/
    seed.py             # seeds default roles/admin user into the platform's auth DB
    *.xlsx              # scenario/golden-dataset working files
  ragas_env/             # local Python venv for services/ragas-service (gitignored)
```

## Quickstart

The evaluation tool lives in `services/ragas-service/`. Full instructions (metrics explained, how retrieval context is captured, per-user RBAC evaluation, output workbook structure) are in **[services/ragas-service/README.md](services/ragas-service/README.md)**.

Fastest way to run it, via Docker:

```powershell
cd services\ragas-service
copy .env.example .env   # fill in your AWS credentials
docker compose up --build
```

Results are written to `services/ragas-service/output/ragas_results.xlsx` on your host machine.

To run a single domain instead of both:

```powershell
docker compose run --rm ragas-evaluation --domain hr
```

## Requirements

- Docker Desktop (recommended path — see above), **or** Python 3.11 with `services/ragas-service/requirements.txt` installed into a local venv
- AWS credentials with access to Bedrock (judge LLM) and the SageMaker embedding endpoint in `ap-southeast-3`
- Network access to the deployed AI Platform API

## Credentials

Never commit `services/ragas-service/.env` — it holds real AWS keys. Copy `.env.example` to `.env` and fill it in locally; it's gitignored.
