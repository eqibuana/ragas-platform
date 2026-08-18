import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit, models, platform_client, ragas_runner, schemas
from ..database import get_db

router = APIRouter(prefix="/api/runs", tags=["runs"])


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token — log in first")
    return authorization.split(" ", 1)[1]


def _allowed_domains_for_roles(roles: list[str]) -> set[str]:
    if "admin" in roles:
        return {"hr", "contact_center"}
    domains: set[str] = set()
    if any(role.startswith("hr_") or role == "hr_user" for role in roles):
        domains.add("hr")
    if any(role.startswith("cc_") or role == "cc_user" for role in roles):
        domains.add("contact_center")
    return domains


@router.post("", response_model=schemas.RunOut, status_code=201)
async def create_run(
    body: schemas.RunCreate,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    token = _extract_bearer(authorization)
    try:
        # Re-validate against the platform (not the client-supplied identity) so the
        # run always executes as whoever the token actually belongs to, right now.
        identity = await platform_client.whoami(token)
    except platform_client.PlatformAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    dataset = db.get(models.ScenarioDataset, body.dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    if not dataset.rows:
        raise HTTPException(status_code=400, detail="Dataset has no rows to evaluate")

    allowed = _allowed_domains_for_roles(identity["roles"])
    if dataset.domain not in allowed:
        raise HTTPException(status_code=403, detail="You are not authorized for this domain")

    run = models.EvaluationRun(
        dataset_id=dataset.id,
        domain=dataset.domain,
        requested_by_email=identity["email"],
        status="pending",
    )
    db.add(run)
    db.flush()  # assigns run.id
    audit.record(
        db,
        actor_email=identity["email"],
        actor_roles=identity["roles"],
        action="run.started",
        domain=dataset.domain,
        dataset_id=dataset.id,
        entity_type="run",
        entity_id=run.id,
        run_id=run.id,
        after={"dataset_id": dataset.id, "row_count": len(dataset.rows)},
    )
    db.commit()
    db.refresh(run)

    # Token is handed to the background job in memory only — never written to the DB.
    ragas_runner._PENDING_TOKENS[run.id] = (token, identity["user_id"], identity["roles"])
    background_tasks.add_task(ragas_runner.run_evaluation, run.id)

    return run


@router.get("", response_model=list[schemas.RunOut])
def list_runs(dataset_id: int | None = None, db: Session = Depends(get_db)):
    query = select(models.EvaluationRun).order_by(models.EvaluationRun.created_at.desc())
    if dataset_id is not None:
        query = query.where(models.EvaluationRun.dataset_id == dataset_id)
    return db.execute(query).scalars().all()


@router.get("/compare", response_model=schemas.RunCompareOut)
def compare_runs(run_a: int, run_b: int, db: Session = Depends(get_db)):
    """Diff two completed runs metric-by-metric, overall and per question.

    Meant for regression tracking: run_a is typically the older/baseline run, run_b
    the newer one, so a positive delta reads as "improved" for every current metric
    (they're all higher-is-better).
    """
    a = db.get(models.EvaluationRun, run_a)
    b = db.get(models.EvaluationRun, run_b)
    if a is None or b is None:
        raise HTTPException(status_code=404, detail="One or both runs not found")
    if a.dataset_id != b.dataset_id:
        raise HTTPException(status_code=400, detail="Runs must belong to the same dataset to be compared")
    if a.status != "completed" or b.status != "completed":
        raise HTTPException(status_code=400, detail="Both runs must be completed to compare")

    summary_a = a.summary or {}
    summary_b = b.summary or {}
    metric_names = sorted(set(summary_a) | set(summary_b))
    summary_deltas = [
        schemas.MetricDelta(
            metric=name,
            run_a=summary_a.get(name),
            run_b=summary_b.get(name),
            delta=(
                round(summary_b[name] - summary_a[name], 4)
                if name in summary_a and name in summary_b
                else None
            ),
        )
        for name in metric_names
    ]

    def _key(result: models.EvaluationRunResult):
        return result.scenario_row_id if result.scenario_row_id is not None else f"q:{result.question}"

    results_a = {_key(r): r for r in a.results}
    results_b = {_key(r): r for r in b.results}
    question_deltas = []
    for key in sorted(set(results_a) | set(results_b), key=str):
        ra = results_a.get(key)
        rb = results_b.get(key)
        metric_deltas = {}
        if ra is not None and rb is not None:
            for name in sorted(set(ra.metrics) & set(rb.metrics)):
                metric_deltas[name] = round(rb.metrics[name] - ra.metrics[name], 4)
        question_deltas.append(
            schemas.QuestionDelta(
                scenario_row_id=(ra or rb).scenario_row_id,
                question=(ra or rb).question,
                run_a_metrics=ra.metrics if ra else None,
                run_b_metrics=rb.metrics if rb else None,
                metric_deltas=metric_deltas,
            )
        )

    return schemas.RunCompareOut(
        run_a=a,
        run_b=b,
        summary_deltas=summary_deltas,
        question_deltas=question_deltas,
    )


@router.post("/{run_id}/cancel", response_model=schemas.RunOut)
async def cancel_run(
    run_id: int,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """Best-effort stop for an in-flight run — see ragas_runner._CANCEL_EVENTS for the
    cooperative-cancellation mechanics (it can only take effect between scenario/metric
    calls, not mid-request)."""
    token = _extract_bearer(authorization)
    try:
        identity = await platform_client.whoami(token)
    except platform_client.PlatformAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    run = db.get(models.EvaluationRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    allowed = _allowed_domains_for_roles(identity["roles"])
    if run.domain not in allowed:
        raise HTTPException(status_code=403, detail="You are not authorized for this domain")

    if run.status in ("completed", "failed", "cancelled"):
        return run

    ragas_runner.request_cancel(run_id)

    if run.status == "pending":
        # Background task hasn't reached its first checkpoint yet (or possibly hasn't been
        # scheduled at all) — flip it to cancelled now rather than leaving the UI waiting on
        # a job that may take a moment to notice the flag.
        run.status = "cancelled"
        run.error_message = "Cancelled by user"
        run.finished_at = datetime.datetime.now(datetime.timezone.utc)
        audit.record(
            db,
            actor_email=identity["email"],
            actor_roles=identity["roles"],
            action="run.cancelled",
            domain=run.domain,
            dataset_id=run.dataset_id,
            entity_type="run",
            entity_id=run.id,
            run_id=run.id,
            after={"status": "cancelled"},
        )
        db.commit()
        db.refresh(run)

    return run


@router.get("/{run_id}", response_model=schemas.RunOut)
def get_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(models.EvaluationRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.get("/{run_id}/results", response_model=list[schemas.RunResultOut])
def get_run_results(run_id: int, db: Session = Depends(get_db)):
    run = db.get(models.EvaluationRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run.results
