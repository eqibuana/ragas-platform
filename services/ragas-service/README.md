# RAGAS Evaluation — PermataBank AI Platform

## Overview

This tool evaluates the RAG (Retrieval-Augmented Generation) pipeline quality using the [RAGAS framework](https://docs.ragas.io/). It sends questions from a golden dataset to the deployed AI platform (a real AWS environment, not local dev), collects answers, then uses a separate judge LLM (Bedrock Nova Pro) to score each answer on multiple quality dimensions.

It's a standalone script — not a FastAPI service — kept under `services/` for repo-layout consistency, but run from its own isolated Python environment (`ragas_env/` at the repo root), not the `uv` workspace the other services share. Its pinned dependency set (`ragas==0.2.15` + specific `langchain`/`pandas`/`numpy` versions) previously broke when mixed with the workspace's shared resolution, so it's intentionally excluded from `[tool.uv.workspace]` in the root `pyproject.toml`.

---

## Directory Layout

```
services/ragas-service/
  ragas_evaluation.py    # the eval script itself
  requirements.txt       # pinned deps for ragas_env (NOT part of the uv workspace)
  ragas_data.xlsx         # golden dataset (HR + Contact Center sheets)
  .env                    # AWS credentials for the judge LLM (gitignored — real secrets)
  .env.example            # template for .env
  Dockerfile              # containerized run (self-contained build context)
  docker-compose.yml       # docker-compose wrapper around the Dockerfile
  output/                  # generated results + run logs (gitignored)
    ragas_results.xlsx
    logs/
      ragas_run_<timestamp>.log
```

---

## How It Works

```
┌──────────────────────────────────────────────────────────────────┐
│  STEP 1: Load Golden Dataset (ragas_data.xlsx)                    │
│  ├── Sheet "HR" → questions + expected answers                    │
│  └── Sheet "Contact Center" → questions + expected answers        │
├──────────────────────────────────────────────────────────────────┤
│  STEP 2: Login to API — once per domain user (see "Users" below)  │
│  ├── HR: hr_mgr_1 + hr_usr_1/2/3 (People Care Agent)              │
│  └── CC: cc_mgr_1 + cc_usr_1/2/3 (Contact Center Agent)           │
├──────────────────────────────────────────────────────────────────┤
│  STEP 3: Send every question to POST /api/v1/chat, per user        │
│  ├── Your full RAG pipeline runs on AWS (stream=false,             │
│  │   include_chunk_text=true)                                     │
│  └── Collects: answer + contexts (real chunk text when the         │
│      deployed service returns it, else per-chunk metadata)         │
├──────────────────────────────────────────────────────────────────┤
│  STEP 4: RAGAS Evaluation (LLM-as-Judge)                           │
│  ├── A SEPARATE LLM (Bedrock Nova Pro) scores each answer,         │
│  │   one metric per isolated ragas.evaluate() call                 │
│  └── Outputs scores + a full derivation trace per metric/question  │
├──────────────────────────────────────────────────────────────────┤
│  STEP 5: Save a 6-sheet workbook + a timestamped run log           │
└──────────────────────────────────────────────────────────────────┘
```

Each metric is scored in its **own isolated `ragas.evaluate()` call** (`_score_one_metric`). This matters: batching all metrics into one call means a single incompatible metric (e.g. Summarization Score, which needs a `reference_contexts` field none of the others use) throws an exception that silently discards every other optional metric too. Isolating them means one failure only removes that one column — the rest of the report still ships.

---

## Users evaluated per domain

The script logs in as **every** credential listed for a domain and re-runs the full question set once per user — this checks RBAC consistency (do manager and regular-user roles get equivalent answers for public/internal content?), not just raw pipeline quality:

| Domain | Users evaluated |
|--------|------------------|
| `hr` | `hr_mgr_1` (hr_manager), `hr_usr_1`, `hr_usr_2`, `hr_usr_3` (hr_user) |
| `contact_center` | `cc_mgr_1` (cc_manager), `cc_usr_1`, `cc_usr_2`, `cc_usr_3` (cc_user) |

So a sheet with N questions produces **4×N** result rows for that domain. Pass `--email`/`--password` together to override this and run as a single custom user instead (skips the per-user loop entirely).

---

## Retrieved context: real chunk text vs. metadata proxy

Every request sends `include_chunk_text: true` (`ChatRequest.include_chunk_text` — a flag that exists in the agentic-chatbot-service API specifically for eval/debug use like this). When the deployed service honors it, `Citation.chunk_text` comes back with the actual retrieved chunk body, and that's what gets scored as `retrieved_contexts`.

**If the deployed `agentic-chatbot-service` hasn't been redeployed with that wiring yet**, `chunk_text` comes back `None` for every citation, and the script falls back — per chunk — to `_build_pseudo_context()`: a readable string built only from citation metadata (`doc_title` / `section_path` / `page_number` / `score` / `chunk_type` / `caption`), never the chunk's real body text. Any content-dependent metric (`faithfulness`, `context_precision`, `context_recall`, `summarization_score`) scored against the proxy is an approximation, not true content grounding — the judge is being shown a label, not the sentence it needs to verify a ground-truth claim against.

The **Data Sources** sheet in every output workbook states, per row, whether that run is relying on real text or the fallback proxy — check it before trusting a low Context Recall/Precision score at face value.

---

## Input Data (Golden Dataset)

**File:** `ragas_data.xlsx` (co-located in this directory; resolved relative to the script's own path, not the current working directory)

Two sheets:

| Sheet | Domain | Agent Used |
|-------|--------|-----------|
| HR | `hr` | People Care Agent |
| Contact Center | `contact_center` | Contact Center Agent |

**Required columns per sheet:**

| Column Name | Maps To | Description |
|-------------|---------|-------------|
| Test Scenario | `question` | The user query to evaluate |
| Expected Result | `ground_truth` | The ideal/correct answer |

---

## RAGAS Inputs Per Question

| Input | Source | Description |
|-------|--------|-------------|
| `question` / `user_input` | Excel "Test Scenario" | The user query |
| `answer` / `response` | RAG API response `answer` field | What the pipeline actually answered |
| `contexts` / `retrieved_contexts` | RAG API `citations[]` | Real chunk text (preferred) or per-chunk metadata proxy (fallback) — see above |
| `ground_truth` / `reference` | Excel "Expected Result" | The ideal answer (human-written) |

---

## Evaluation Metrics

### 1. Faithfulness (Higher = Better)

**Measures:** Hallucination — is the answer grounded in the retrieved context?

**How it calculates:**

1. The judge LLM breaks the answer into individual claims
2. For each claim, checks if it's supported by the retrieved context
3. Score = supported claims / total claims

**Example:**
```
Answer: "SR021 min order Rp1M, offering period Aug-Sep 2024"
Claims: ["min order Rp1M", "offering period Aug-Sep 2024"]

Context contains "min order Rp1M"? → YES
Context contains "offering Aug-Sep 2024"? → YES

Score = 2/2 = 1.0
```

| Score | Meaning |
|-------|---------|
| 1.0 | Every statement backed by documents |
| 0.5 | Half the statements are hallucinated |
| 0.0 | Completely made up |

---

### 2. Answer Relevancy (Higher = Better)

**Measures:** Does the answer actually address the question asked?

**How it calculates:**

1. The judge LLM generates hypothetical questions that the answer would satisfy, and separately judges whether the answer is "noncommittal" (a hedge/refusal/non-answer)
2. If judged noncommittal, the score is forced to exactly `0.0` regardless of embedding similarity
3. Otherwise: compares generated questions to the original question (cosine similarity) and averages

**Example:**
```
Original question: "Apakah terdapat pembatasan pemesanan SR021?"
Answer: "Min order SR021 is Rp1M, max Rp5B"

Generated questions from answer:
  - "What is the minimum order for SR021?" → similarity 0.85
  - "What are the order limits?" → similarity 0.90
  - "How much can I order SR021?" → similarity 0.80

Score = average(0.85, 0.90, 0.80) = 0.85
```

| Score | Meaning |
|-------|---------|
| 1.0 | Answer directly addresses the question |
| 0.5 | Answer is partially relevant |
| 0.0 | Answer is off-topic **or** judged noncommittal — check the answer text before assuming off-topic; a substantive, correctly-cited answer can still land exactly on 0.0 if the judge's noncommittal classifier misfires on incidental phrasing (e.g. a trailing "let me know if you want more detail" offer) |

---

### 3. Context Precision (Higher = Better)

**Measures:** Are the retrieved documents relevant? (Retrieval quality)

**How it calculates:**

1. The judge LLM evaluates each retrieved chunk: "Is this relevant to the question / ground truth?"
2. Scores based on ranking position (relevant docs at top = better)
3. Uses weighted precision at each rank position

**Example:**
```
Question: "Kapan masa penawaran SR021?"

Retrieved chunks:
  [1] "FAQ SR021 - masa penawaran 23 Agustus..." → RELEVANT ✓
  [2] "FAQ SR020 - something about SR020..." → NOT RELEVANT ✗
  [3] "FAQ SR021 - minimum pemesanan..." → NOT RELEVANT ✗

Score = precision weighted by rank position
```

| Score | Meaning |
|-------|---------|
| 1.0 | All retrieved docs are relevant, well-ranked |
| 0.5 | Mix of relevant and irrelevant docs |
| 0.0 | Retrieved docs don't help answer the question |

**Caveat:** if `retrieved_contexts` is running on the metadata-proxy fallback (see above), a chunk can pass this judge's relevance bar purely on topical/title match (e.g. a section literally named after the question's topic) without containing any real supporting content — precision scores can read misleadingly high in that mode. Check the Data Sources sheet.

---

### 4. Context Recall (Higher = Better)

**Measures:** Did retrieval find ALL information needed? (Completeness)

**How it calculates:**

1. The judge LLM breaks the ground_truth into individual facts
2. For each fact, checks if it's present in the retrieved context
3. Score = facts found / total facts needed

**Example:**
```
Ground truth: "Min order Rp1M, max Rp5B, offering Aug 23 - Sep 18 2024"
Facts: ["min Rp1M", "max Rp5B", "Aug 23 - Sep 18 2024"]

Context contains "min Rp1M"? → YES ✓
Context contains "max Rp5B"? → NO ✗ (not retrieved!)
Context contains "Aug 23 - Sep 18"? → YES ✓

Score = 2/3 = 0.67
```

| Score | Meaning |
|-------|---------|
| 1.0 | All needed info was retrieved |
| 0.5 | Important information is missing |
| 0.0 | Nothing useful was retrieved |

**Caveat:** unlike Context Precision, this metric needs the actual sentence, not a topic label — it's the metric hit hardest by the metadata-proxy fallback. A `0.0` here while a real chunk with a high retrieval score clearly covers the topic is the signature of running on the proxy, not a genuine retrieval gap. Check Data Sources before concluding the knowledge base is missing content.

---

### 5. Answer Correctness (Higher = Better)

**Measures:** Does the answer match the expected answer?

**How it calculates:**

1. Compares answer statements vs ground_truth statements:
   - TP (True Positive): correct facts present
   - FP (False Positive): wrong facts added
   - FN (False Negative): correct facts missing
2. Calculates F1 score + semantic similarity
3. Final score = weighted combination (0.75 factual + 0.25 semantic)

| Score | Meaning |
|-------|---------|
| 1.0 | Answer perfectly matches expected |
| 0.5 | Partially correct |
| 0.0 | Completely wrong |

---

### 6. Noise Sensitivity (Lower = Better)

**Measures:** Does irrelevant context confuse the model?

**How it calculates:**

1. Checks if the model's answer is influenced by irrelevant retrieved documents
2. Measures how robust the answer is when "noise" is present in context

| Score | Meaning |
|-------|---------|
| 0.0 | Robust — never confused by noise |
| 1.0 | Fragile — easily confused by irrelevant docs |

---

### 7. Summarization Score (Higher = Better)

**Measures:** How well the answer captures the key information present in the retrieved chunks.

**How it calculates:** uses a *different* RAGAS input schema than the other metrics (`response` + `reference_contexts`, not `retrieved_contexts`) — this is why it's computed in its own separate `ragas.evaluate()` call against its own mini-dataset, rather than being folded into the main metric loop.

| Score | Meaning |
|-------|---------|
| 1.0 | Answer captures all key info from contexts |
| 0.5 | Some key info missing |
| 0.0 | Answer poorly summarizes retrieved information |

---

## Usage

**One-time setup — activate the isolated venv** (from repo root):
```powershell
.\ragas_env\Scripts\Activate.ps1
```
Or create it fresh if it doesn't exist yet:
```powershell
python -m venv ragas_env
.\ragas_env\Scripts\Activate.ps1
pip install -r services\ragas-service\requirements.txt
```

**Credentials:** copy `.env.example` to `.env` in this directory and fill in real AWS credentials (used only for the Bedrock judge LLM + SageMaker embeddings — never for the RAG pipeline API calls themselves, which use the Excel-driven login users instead).

**Run both domains (from repo root):**
```powershell
python services\ragas-service\ragas_evaluation.py
```

**Run Contact Center only:**
```powershell
python services\ragas-service\ragas_evaluation.py --domain contact_center
```

**Run HR only:**
```powershell
python services\ragas-service\ragas_evaluation.py --domain hr
```

**Run as a single custom user** (skips the per-domain-user loop described above):
```powershell
python services\ragas-service\ragas_evaluation.py --email someone@example.com --password ...
```

**Custom output file / dataset / API target:**
```powershell
python services\ragas-service\ragas_evaluation.py --output my_results.xlsx --excel my_dataset.xlsx --api-url http://localhost:8000
```

**Custom run-log directory** (default: `logs/` next to `--output`):
```powershell
python services\ragas-service\ragas_evaluation.py --log-dir C:\path\to\logs
```

### Docker

```powershell
cd services\ragas-service
docker compose up --build
```

Builds from this directory (`context: .`), mounts `./output` for results and `./ragas_data.xlsx` read-only. `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` are interpolated from `./.env` automatically (docker-compose loads a `.env` file in the same directory as `docker-compose.yml` for `${VAR}` substitution) — no extra flags needed as long as `.env` is filled in.

---

## Output

Every run produces **one workbook** (`output/ragas_results.xlsx` by default) with 6 sheets, plus a timestamped run log (`output/logs/ragas_run_<YYYYMMDD_HHMMSS>.log`) capturing the full console output for later reference.

| Sheet | Contents |
|-------|----------|
| **Summary** | One row per domain + an OVERALL row: question count, avg latency, avg score per metric that actually computed this run. A note lists any metric that failed to compute (see the run log for why). |
| **Results** | The full per-question, per-user results grid — question, ground truth, answer, retrieved contexts (real text or metadata proxy), per-chunk retrieval detail, every metric's score + qualitative assessment + a comparison log, latency, domain/agent/user. |
| **Formulas** | A static reference table: exact formula, comparison basis, RAGAS input fields, data source, score range, and pass threshold for every metric — read this before interpreting any score. |
| **Trace** | Row-by-row derivation detail per metric: for Faithfulness, the actual claims extracted and their SUPPORTED/NOT SUPPORTED verdicts; for Context Recall, which ground-truth statements were attributed to context and which weren't; for Answer Correctness, the TP/FP/FN claim breakdown — the "show your work" behind every score. |
| **Comparison History** | The full text-to-text comparisons per dimension (Answer vs Context, Answer vs Ground Truth, Context vs Question) so you can see exactly what the judge LLM was shown. |
| **Data Sources** | What each RAGAS field actually contains, where it comes from, which metrics consume it, and — critically — whether `retrieved_contexts` is real chunk text or the metadata-proxy fallback for this run. |

Key **Results** columns (friendly headers in the actual workbook):

| Column | Description |
|--------|-------------|
| Question / Ground Truth / Answer (Generated by Agent) | The three human-readable core fields |
| RAGAS retrieved_contexts (= KB Chunks, real text or metadata proxy) | What was actually scored — see "Retrieved context" section above |
| Retrieved Chunks Detail | Per-chunk service/doc/section/page/type/score, unabbreviated — for tracing a score back to an exact chunk |
| Retrieval Warning | Flags rows where the agent returned zero citations (a real retrieval gap) |
| Faithfulness / Answer Relevancy / Context Precision / Context Recall / Answer Correctness / Noise Sensitivity / Summarization Score | Score + Assessment + Log columns for each |
| Latency (s) | Wall-clock time for the `POST /api/v1/chat` call |

---

## Interpreting Results

### Good Scores (target)

| Metric | Target | Action if low |
|--------|--------|---------------|
| Faithfulness | > 0.8 | Check Data Sources first (proxy fallback?); if real text, tighten system prompt hallucination rules |
| Answer Relevancy | > 0.8 | Check the answer text for the row before assuming a real problem — an exact 0.0 on a substantive answer often means the noncommittal classifier misfired, not that the answer is off-topic |
| Context Precision | > 0.7 | Check Data Sources first (proxy fallback can score misleadingly high); if real text, tune expand_queries prompt |
| Context Recall | > 0.7 | Check Data Sources first — this is the metric most affected by the metadata-proxy fallback; if real text, upload more documents / improve chunking |
| Answer Correctness | > 0.7 | Improve generate prompt structure |
| Noise Sensitivity | < 0.3 | Strengthen "answer ONLY from context" rules |
| Summarization Score | > 0.8 | Improve generation prompt's use of retrieved content |

### Diagnosing Issues

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Context Recall/Precision/Faithfulness all suspiciously low despite good answers | `retrieved_contexts` running on the metadata-proxy fallback | Redeploy `agentic-chatbot-service` with the `include_chunk_text` wiring (`build_initial_state` → `generate._extract_citations`) so real chunk text comes back |
| Answer Relevancy = 0.0 on an otherwise correct, on-topic answer | RAGAS's noncommittal-answer classifier misfiring on boilerplate phrasing | Tighten the domain's `generate.system_prompt` (via `/admin/domain-config`, no redeploy needed) to forbid "offer more detail" / meta-commentary tails |
| Low faithfulness, high relevancy | Model hallucinating beyond context | Tighten generate prompt security section |
| High faithfulness, low relevancy | Answer is grounded but off-topic | Improve decompose_query prompt |
| Low context precision (confirmed real text, not proxy) | Retrieval brings irrelevant docs | Improve expand_queries terminology |
| Low context recall (confirmed real text, not proxy) | Missing documents in KB | Upload more documents, check chunking |
| Low correctness, others high | Answer format differs from ground_truth | Adjust ground_truth format or generate structure |

---

## Configuration

All configuration is at the top of `ragas_evaluation.py`. Paths are resolved against the script's own directory (`SERVICE_DIR = Path(__file__).parent`), not the current working directory, so it behaves the same whether run from repo root, from inside this directory, or in Docker:

```python
API_URL = "http://k8s-aiplatfo-frontend-...amazonaws.com"     # deployed AI Platform API
EXCEL_PATH = str(SERVICE_DIR / "ragas_data.xlsx")               # golden dataset
OUTPUT_PATH = str(SERVICE_DIR / "output" / "ragas_results.xlsx")  # results workbook

CREDENTIALS = {
    "hr": [
        {"email": "hr_mgr_1@example.com", "role": "hr_manager"},
        {"email": "hr_usr_1@example.com", "role": "hr_user"},
        {"email": "hr_usr_2@example.com", "role": "hr_user"},
        {"email": "hr_usr_3@example.com", "role": "hr_user"},
    ],
    "contact_center": [
        {"email": "cc_mgr_1@example.com", "role": "cc_manager"},
        {"email": "cc_usr_1@example.com", "role": "cc_user"},
        {"email": "cc_usr_2@example.com", "role": "cc_user"},
        {"email": "cc_usr_3@example.com", "role": "cc_user"},
    ],
}

SHEET_TO_DOMAIN = {
    "HR": "hr",                          # → People Care Agent
    "Contact Center": "contact_center",  # → Contact Center Agent
}
```

AWS credentials for the judge LLM come from `services/ragas-service/.env` (gitignored — copy `.env.example` and fill in real values), loaded via `python-dotenv` at import time.

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `RAGAS NOT USABLE` | `ragas` not installed in the active env | Activate `ragas_env` and `pip install -r requirements.txt`, or `uv pip install ragas datasets langchain-aws` |
| `No module langchain_community.chat_models.vertexai` | Missing dependency | `pip install langchain-google-vertexai` |
| `AccessDeniedException` | IAM user lacks Bedrock permissions | Add `AmazonBedrockFullAccess` policy |
| `model identifier is invalid` | Embeddings model not in region | Already handled — falls back to LLM-only scoring for affected metrics |
| `504 Gateway Timeout` | RAG pipeline too slow / SageMaker models cold | Warm up SageMaker models first (Platform Health admin page "Warm up" button) |
| `getaddrinfo failed` | No internet / DNS failure | Check VPN/internet connection |
| `retrieved_contexts` always shows the metadata-proxy format, never real text | Deployed `agentic-chatbot-service` hasn't been redeployed with `include_chunk_text` wiring yet | Redeploy; this script already sends the flag on every request and will pick up real text automatically once the service honors it |
