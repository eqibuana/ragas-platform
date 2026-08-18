"""Background job that scores a scenario dataset with RAGAS, as the logged-in user,
against the live AI Platform.

Follows services/ragas-service/ragas_evaluation.py's scoring core as closely as this
single-user, on-demand context allows: same Bedrock Nova Pro judge + SageMaker bge-m3
embeddings (_setup_judge), same 7 metrics (faithfulness, answer_relevancy,
context_precision, context_recall, answer_correctness, noise_sensitivity,
summarization_score — the last needing its own response+reference_contexts dataset,
same as that script), same per-metric-isolated ragas.evaluate() calls so one metric's
failure never wipes out the others. Trimmed of that script's trace/history-log
extraction: this gives per-row scores + averages for the web UI, not the full
diagnostic workbook the CLI tool produces — see that tool if a deeper trace is needed.
"""

import datetime
import threading
import time
import traceback

import pandas as pd

from . import audit, models, platform_client
from .config import settings
from .database import SessionLocal

METRIC_LOG_PREFIX = "[ragas_runner]"

# run_id -> (access_token, user_id, roles), handed off by routers/runs.py right before
# scheduling the background task and consumed (then discarded) here. Never persisted —
# platform tokens don't belong in the database.
_PENDING_TOKENS: dict[int, tuple[str, str, list[str]]] = {}

# run_id -> cooperative cancel flag. Cancellation is best-effort: the runner only checks
# this between scenario-answer calls and between metric-scoring calls, so a run already
# blocked inside a single Bedrock/platform HTTP request finishes that call before it stops.
_CANCEL_EVENTS: dict[int, threading.Event] = {}


def request_cancel(run_id: int) -> None:
    """Called from routers/runs.py when a user hits Stop on an active run."""
    _CANCEL_EVENTS.setdefault(run_id, threading.Event()).set()


def _mark_cancelled(db, run: models.EvaluationRun, roles: list[str]) -> None:
    run.status = "cancelled"
    run.error_message = "Cancelled by user"
    run.finished_at = datetime.datetime.now(datetime.timezone.utc)
    audit.record(
        db,
        actor_email=run.requested_by_email,
        actor_roles=roles,
        action="run.cancelled",
        domain=run.domain,
        dataset_id=run.dataset_id,
        entity_type="run",
        entity_id=run.id,
        run_id=run.id,
        after={"status": "cancelled"},
    )
    db.commit()


def _setup_judge():
    """Bedrock Nova Pro judge LLM + SageMaker bge-m3 embeddings, wrapped for ragas 0.2.x.

    If the app is intentionally started without AWS judge credentials, return (None, None)
    so the UI can still use the platform login to fetch answers while skipping metric scoring
    instead of crashing the whole run.
    """
    if not settings.aws_access_key_id or not settings.aws_secret_access_key:
        print(
            f"{METRIC_LOG_PREFIX} AWS judge credentials not configured; skipping RAGAS metric scoring. "
            "The platform login flow still works, but this run will not compute Bedrock-based metrics."
        )
        return None, None

    try:
        import boto3
        from langchain_aws import ChatBedrockConverse
        from langchain_core.embeddings import Embeddings
        from ragas.embeddings import LangchainEmbeddingsWrapper
        from ragas.llms import LangchainLLMWrapper

        bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=settings.aws_region,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )
        raw_llm = ChatBedrockConverse(
            model="amazon.nova-pro-v1:0",
            region_name=settings.aws_region,
            client=bedrock_client,
            temperature=0,
        )
        llm = LangchainLLMWrapper(raw_llm)

        embeddings = None
        try:
            import json as _json

            class SageMakerBgeEmbeddings(Embeddings):
                def __init__(self):
                    self._client = boto3.client(
                        "sagemaker-runtime",
                        region_name=settings.aws_region,
                        aws_access_key_id=settings.aws_access_key_id,
                        aws_secret_access_key=settings.aws_secret_access_key,
                    )
                    self.endpoint_name = "bge-m3-embedding"
                    self.ic_name = "bge-m3-ic"

                def _invoke(self, texts):
                    kwargs = {
                        "EndpointName": self.endpoint_name,
                        "ContentType": "application/json",
                        "Body": _json.dumps({"inputs": texts, "truncate": True}),
                    }
                    if self.ic_name:
                        kwargs["InferenceComponentName"] = self.ic_name
                    resp = self._client.invoke_endpoint(**kwargs)
                    return _json.loads(resp["Body"].read())

                def embed_documents(self, texts):
                    if not texts:
                        return []
                    results = []
                    for i in range(0, len(texts), 16):
                        results.extend(self._invoke(texts[i : i + 16]))
                    return results

                def embed_query(self, text):
                    return self._invoke([text])[0]

            raw_embeddings = SageMakerBgeEmbeddings()
            raw_embeddings.embed_query("test")
            embeddings = LangchainEmbeddingsWrapper(raw_embeddings)
        except Exception as e:
            print(f"{METRIC_LOG_PREFIX} embeddings unavailable ({e}); answer_relevancy/answer_correctness will be skipped")

        return llm, embeddings
    except Exception as e:
        print(
            f"{METRIC_LOG_PREFIX} AWS judge unavailable ({e}); skipping RAGAS metric scoring. "
            "The app still used the website login to fetch chat answers."
        )
        return None, None


def _build_metric_specs():
    from ragas.metrics import (
        answer_correctness,
        answer_relevancy,
        context_precision,
        context_recall,
        faithfulness,
    )

    specs = [
        ("faithfulness", faithfulness),
        ("answer_relevancy", answer_relevancy),
        ("context_precision", context_precision),
        ("context_recall", context_recall),
        ("answer_correctness", answer_correctness),
    ]

    try:
        from ragas.metrics import NoiseSensitivity

        specs.append(("noise_sensitivity", NoiseSensitivity()))
    except (ImportError, AttributeError):
        print(f"{METRIC_LOG_PREFIX} noise_sensitivity not available in this ragas version")

    return specs


def _build_summarization_metric():
    """Same fallback chain as ragas_evaluation.py: SummarizationScore (class) in newer
    ragas, summarization_score (pre-built instance) in older ones."""
    try:
        from ragas.metrics import SummarizationScore

        return SummarizationScore()
    except ImportError:
        try:
            from ragas.metrics import summarization_score

            return summarization_score
        except (ImportError, AttributeError):
            print(f"{METRIC_LOG_PREFIX} summarization_score not available in this ragas version")
            return None


def _score_one_metric(name, metric, dataset, llm, embeddings) -> list | None:
    from ragas import evaluate as ragas_evaluate

    kwargs = {"llm": llm}
    if hasattr(metric, "llm"):
        metric.llm = llm
    if embeddings is not None:
        kwargs["embeddings"] = embeddings
        if hasattr(metric, "embeddings"):
            metric.embeddings = embeddings

    try:
        result = ragas_evaluate(dataset, metrics=[metric], **kwargs)
        scores_df = result.to_pandas()
        input_cols = set(dataset.column_names)
        col = name if name in scores_df.columns else next(
            (c for c in scores_df.columns if c not in input_cols), None
        )
        if col is None:
            print(f"{METRIC_LOG_PREFIX} [{name}] FAILED (no score column returned)")
            return None
        print(f"{METRIC_LOG_PREFIX} [{name}] OK")
        return scores_df[col].tolist()
    except Exception as e:
        print(f"{METRIC_LOG_PREFIX} [{name}] FAILED ({e})")
        traceback.print_exc()
        return None


def run_evaluation(run_id: int) -> None:
    """Entry point handed to FastAPI's BackgroundTasks in routers/runs.py.

    Defined as a plain `def` (not `async def`) on purpose: Starlette runs sync
    BackgroundTasks callables in a worker thread, so this long-running, blocking
    (boto3 + httpx are both sync) job never blocks the event loop that's serving
    other requests — including the client polling GET /api/runs/{id} for this run.
    """
    db = SessionLocal()
    roles: list[str] = []
    try:
        run = db.get(models.EvaluationRun, run_id)
        if run is None:
            return

        run.status = "running"
        run.started_at = datetime.datetime.now(datetime.timezone.utc)
        db.commit()

        dataset_obj = db.get(models.ScenarioDataset, run.dataset_id)
        rows = list(dataset_obj.rows)
        access_token, user_id, roles = _PENDING_TOKENS.pop(run_id)
        cancel_event = _CANCEL_EVENTS.setdefault(run_id, threading.Event())

        questions, answers, contexts_list, ground_truths, latencies = [], [], [], [], []
        for row in rows:
            if cancel_event.is_set():
                break
            start = time.time()
            try:
                result = platform_client.ask_sync(access_token, user_id, roles, row.test_scenario, run.domain)
                answers.append(result["answer"])
                contexts_list.append(result["contexts"])
            except Exception as e:
                answers.append(f"ERROR: {e}")
                contexts_list.append(["No context - API error"])
            latencies.append(time.time() - start)
            questions.append(row.test_scenario)
            ground_truths.append(row.expected_result)

        if cancel_event.is_set():
            _mark_cancelled(db, run, roles)
            return

        from datasets import Dataset

        ragas_dataset = Dataset.from_dict(
            {
                "user_input": questions,
                "response": answers,
                "retrieved_contexts": contexts_list,
                "reference": ground_truths,
            }
        )

        llm, embeddings = _setup_judge()
        computed: dict[str, list] = {}

        if llm is None:
            print(
                f"{METRIC_LOG_PREFIX} Skipping all RAGAS metric scoring because no AWS judge credentials were configured. "
                "The platform login flow still fetched answers; only the chat results were stored."
            )
            run.summary = {
                "status": "skipped",
                "reason": "RAGAS judge disabled: no AWS Bedrock/SageMaker credentials configured",
            }
        else:
            for name, metric in _build_metric_specs():
                if cancel_event.is_set():
                    break
                values = _score_one_metric(name, metric, ragas_dataset, llm, embeddings)
                if values is not None:
                    computed[name] = values

            # Summarization Score needs a DIFFERENT schema (response + reference_contexts,
            # no user_input/reference) — same reason ragas_evaluation.py scores it against
            # its own dataset instead of the shared one: batching it with the rest would
            # raise on the missing field and take every other metric down with it.
            summarization_metric = _build_summarization_metric()
            if summarization_metric is not None and not cancel_event.is_set():
                summ_dataset = Dataset.from_dict({"response": answers, "reference_contexts": contexts_list})
                values = _score_one_metric("summarization_score", summarization_metric, summ_dataset, llm, embeddings)
                if values is not None:
                    computed["summarization_score"] = values

            if cancel_event.is_set():
                _mark_cancelled(db, run, roles)
                return

            run.summary = {
                name: round(float(pd.Series(values).mean(skipna=True)), 4) for name, values in computed.items()
            }

        for i, row in enumerate(rows):
            row_metrics = {
                name: round(float(values[i]), 4)
                for name, values in computed.items()
                if not pd.isna(values[i])
            }
            db.add(
                models.EvaluationRunResult(
                    run_id=run.id,
                    scenario_row_id=row.id,
                    question=questions[i],
                    ground_truth=ground_truths[i],
                    answer=answers[i],
                    contexts=" | ".join(str(c) for c in contexts_list[i]),
                    metrics=row_metrics,
                    latency_seconds=latencies[i],
                )
            )

        run.status = "completed"
        run.finished_at = datetime.datetime.now(datetime.timezone.utc)
        audit.record(
            db,
            actor_email=run.requested_by_email,
            actor_roles=roles,
            action="run.completed",
            domain=run.domain,
            dataset_id=run.dataset_id,
            entity_type="run",
            entity_id=run.id,
            run_id=run.id,
            after={"status": "completed", "summary": run.summary},
        )
        db.commit()
    except Exception as e:
        db.rollback()
        run = db.get(models.EvaluationRun, run_id)
        if run is not None:
            run.status = "failed"
            run.error_message = str(e)
            run.finished_at = datetime.datetime.now(datetime.timezone.utc)
            audit.record(
                db,
                actor_email=run.requested_by_email,
                actor_roles=roles,
                action="run.failed",
                domain=run.domain,
                dataset_id=run.dataset_id,
                entity_type="run",
                entity_id=run.id,
                run_id=run.id,
                after={"status": "failed", "error": str(e)},
            )
            db.commit()
        traceback.print_exc()
    finally:
        _CANCEL_EVENTS.pop(run_id, None)
        db.close()
