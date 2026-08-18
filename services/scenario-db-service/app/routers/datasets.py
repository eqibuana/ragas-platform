from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit, models, platform_client, schemas
from ..database import get_db
from ..excel_parser import parse_scenario_excel

router = APIRouter(prefix="/api/datasets", tags=["datasets"])

ALLOWED_DOMAINS = {"hr", "contact_center"}


def _validate_domain(domain: str) -> None:
    if domain not in ALLOWED_DOMAINS:
        raise HTTPException(status_code=400, detail=f"domain must be one of {sorted(ALLOWED_DOMAINS)}")


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token — log in first")
    return authorization.split(" ", 1)[1]


async def _get_identity(authorization: str | None) -> dict:
    token = _extract_bearer(authorization)
    try:
        return await platform_client.whoami(token)
    except platform_client.PlatformAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


def _allowed_domains_for_roles(roles: list[str]) -> set[str]:
    if "admin" in roles:
        return ALLOWED_DOMAINS
    domains: set[str] = set()
    if any(role.startswith("hr_") or role == "hr_user" for role in roles):
        domains.add("hr")
    if any(role.startswith("cc_") or role == "cc_user" for role in roles):
        domains.add("contact_center")
    return domains


def _ensure_domain_allowed(domain: str, roles: list[str]) -> None:
    allowed = _allowed_domains_for_roles(roles)
    if domain not in allowed:
        raise HTTPException(status_code=403, detail="You are not authorized for this domain")


def _ensure_dataset_domain(dataset: models.ScenarioDataset, roles: list[str]) -> None:
    if dataset.domain not in _allowed_domains_for_roles(roles):
        raise HTTPException(status_code=403, detail="You are not authorized for this domain")


@router.post("", response_model=schemas.ScenarioDatasetOut, status_code=201)
async def upload_dataset(
    file: UploadFile = File(...),
    domain: str = Form(...),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    _validate_domain(domain)
    identity = await _get_identity(authorization)
    _ensure_domain_allowed(domain, identity["roles"])
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Only .xlsx/.xls files are supported")

    content = await file.read()
    rows = parse_scenario_excel(content, domain)
    if not rows:
        raise HTTPException(status_code=400, detail="No usable rows found in the uploaded file")

    dataset = models.ScenarioDataset(domain=domain, source_filename=file.filename)
    dataset.rows = [models.ScenarioRow(**row) for row in rows]
    db.add(dataset)
    db.flush()  # assigns dataset.id so the audit entry can reference it
    audit.record(
        db,
        actor_email=identity["email"],
        actor_roles=identity["roles"],
        action="dataset.uploaded",
        domain=domain,
        dataset_id=dataset.id,
        entity_type="dataset",
        entity_id=dataset.id,
        after={"source_filename": file.filename, "row_count": len(rows)},
    )
    db.commit()
    db.refresh(dataset)
    return dataset


@router.post("/manual", response_model=schemas.ScenarioDatasetOut, status_code=201)
async def create_manual_dataset(
    body: schemas.ManualDatasetCreate,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    _validate_domain(body.domain)
    identity = await _get_identity(authorization)
    _ensure_domain_allowed(body.domain, identity["roles"])
    dataset = models.ScenarioDataset(domain=body.domain, source_filename=body.name)
    db.add(dataset)
    db.flush()
    audit.record(
        db,
        actor_email=identity["email"],
        actor_roles=identity["roles"],
        action="dataset.manual_created",
        domain=body.domain,
        dataset_id=dataset.id,
        entity_type="dataset",
        entity_id=dataset.id,
        after={"source_filename": body.name},
    )
    db.commit()
    db.refresh(dataset)
    return dataset


@router.get("", response_model=list[schemas.ScenarioDatasetSummary])
async def list_datasets(
    domain: str | None = None,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    identity = await _get_identity(authorization)
    allowed_domains = _allowed_domains_for_roles(identity["roles"])
    if domain is not None:
        _validate_domain(domain)
        _ensure_domain_allowed(domain, identity["roles"])
    elif allowed_domains != ALLOWED_DOMAINS:
        domain = next(iter(allowed_domains))

    query = (
        select(models.ScenarioDataset, func.count(models.ScenarioRow.id).label("row_count"))
        .outerjoin(models.ScenarioRow)
        .group_by(models.ScenarioDataset.id)
        .order_by(models.ScenarioDataset.uploaded_at.desc())
    )
    if domain is not None:
        query = query.where(models.ScenarioDataset.domain == domain)

    results = db.execute(query).all()
    return [
        schemas.ScenarioDatasetSummary(
            id=dataset.id,
            domain=dataset.domain,
            source_filename=dataset.source_filename,
            uploaded_at=dataset.uploaded_at,
            row_count=row_count,
        )
        for dataset, row_count in results
    ]


@router.get("/{dataset_id}", response_model=schemas.ScenarioDatasetOut)
async def get_dataset(dataset_id: int, authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    identity = await _get_identity(authorization)
    dataset = db.get(models.ScenarioDataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    _ensure_dataset_domain(dataset, identity["roles"])
    return dataset


@router.put("/{dataset_id}/rows", response_model=schemas.ScenarioDatasetOut)
async def update_rows(
    dataset_id: int,
    updates: list[schemas.ScenarioRowUpdate],
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    identity = await _get_identity(authorization)
    dataset = db.get(models.ScenarioDataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    _ensure_dataset_domain(dataset, identity["roles"])

    rows_by_id = {row.id: row for row in dataset.rows}
    for update in updates:
        row = rows_by_id.get(update.id)
        if row is None:
            raise HTTPException(
                status_code=400, detail=f"Row {update.id} does not belong to dataset {dataset_id}"
            )
        before = {"test_scenario": row.test_scenario, "expected_result": row.expected_result}
        after = {"test_scenario": update.test_scenario, "expected_result": update.expected_result}
        if before != after:
            audit.record(
                db,
                actor_email=identity["email"],
                actor_roles=identity["roles"],
                action="row.updated",
                domain=dataset.domain,
                dataset_id=dataset.id,
                entity_type="row",
                entity_id=row.id,
                before=before,
                after=after,
            )
        row.test_scenario = update.test_scenario
        row.expected_result = update.expected_result

    db.commit()
    db.refresh(dataset)
    return dataset


@router.post("/{dataset_id}/rows", response_model=schemas.ScenarioDatasetOut, status_code=201)
async def add_row(
    dataset_id: int,
    body: schemas.ScenarioRowCreate,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    identity = await _get_identity(authorization)
    dataset = db.get(models.ScenarioDataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    _ensure_dataset_domain(dataset, identity["roles"])

    next_index = max((row.row_index for row in dataset.rows), default=-1) + 1
    new_row = models.ScenarioRow(
        row_index=next_index,
        test_scenario=body.test_scenario,
        expected_result=body.expected_result,
    )
    dataset.rows.append(new_row)
    db.flush()  # assigns new_row.id
    audit.record(
        db,
        actor_email=identity["email"],
        actor_roles=identity["roles"],
        action="row.added",
        domain=dataset.domain,
        dataset_id=dataset.id,
        entity_type="row",
        entity_id=new_row.id,
        after={"test_scenario": new_row.test_scenario, "expected_result": new_row.expected_result},
    )
    db.commit()
    db.refresh(dataset)
    return dataset


@router.delete("/{dataset_id}/rows/{row_id}", response_model=schemas.ScenarioDatasetOut)
async def delete_row(
    dataset_id: int,
    row_id: int,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    identity = await _get_identity(authorization)
    dataset = db.get(models.ScenarioDataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    _ensure_dataset_domain(dataset, identity["roles"])

    row = next((r for r in dataset.rows if r.id == row_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail="Row not found in this dataset")

    audit.record(
        db,
        actor_email=identity["email"],
        actor_roles=identity["roles"],
        action="row.deleted",
        domain=dataset.domain,
        dataset_id=dataset.id,
        entity_type="row",
        entity_id=row.id,
        before={"test_scenario": row.test_scenario, "expected_result": row.expected_result},
    )
    dataset.rows.remove(row)
    db.delete(row)
    db.commit()
    db.refresh(dataset)
    return dataset


@router.post("/{dataset_id}/rows/rollback", response_model=schemas.ScenarioDatasetOut)
async def rollback_row(
    dataset_id: int,
    body: schemas.RowRollbackRequest,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """Revert a single row to the state captured by one of its own audit-log entries.

    row.updated -> restores the row's fields to that entry's `before`.
    row.added   -> that entry created the row, so rolling it back deletes it.
    row.deleted -> that entry removed the row, so rolling it back recreates it (as a new
                   row id — the original id is gone for good, same as any other add).
    """
    identity = await _get_identity(authorization)
    dataset = db.get(models.ScenarioDataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    _ensure_dataset_domain(dataset, identity["roles"])

    entry = db.get(models.AuditLog, body.audit_log_id)
    if (
        entry is None
        or entry.entity_type != "row"
        or entry.dataset_id != dataset_id
        or entry.action not in ("row.updated", "row.added", "row.deleted")
    ):
        raise HTTPException(status_code=404, detail="No matching row change found to roll back")

    rows_by_id = {row.id: row for row in dataset.rows}

    if entry.action == "row.updated":
        row = rows_by_id.get(entry.entity_id)
        if row is None:
            raise HTTPException(status_code=409, detail="That row no longer exists — it may have been deleted")
        before = {"test_scenario": row.test_scenario, "expected_result": row.expected_result}
        restored = entry.before or {}
        row.test_scenario = restored.get("test_scenario", row.test_scenario)
        row.expected_result = restored.get("expected_result", row.expected_result)
        after = {"test_scenario": row.test_scenario, "expected_result": row.expected_result}
        target_row_id = row.id
    elif entry.action == "row.added":
        row = rows_by_id.get(entry.entity_id)
        if row is None:
            raise HTTPException(status_code=409, detail="That row was already removed")
        before = {"test_scenario": row.test_scenario, "expected_result": row.expected_result}
        after = None
        target_row_id = row.id
        dataset.rows.remove(row)
        db.delete(row)
    else:  # row.deleted
        restored = entry.before or {}
        before = None
        next_index = max((r.row_index for r in dataset.rows), default=-1) + 1
        new_row = models.ScenarioRow(
            row_index=next_index,
            test_scenario=restored.get("test_scenario", ""),
            expected_result=restored.get("expected_result", ""),
        )
        dataset.rows.append(new_row)
        db.flush()  # assigns new_row.id
        after = {"test_scenario": new_row.test_scenario, "expected_result": new_row.expected_result}
        target_row_id = new_row.id

    audit.record(
        db,
        actor_email=identity["email"],
        actor_roles=identity["roles"],
        action="row.rolled_back",
        domain=dataset.domain,
        dataset_id=dataset.id,
        entity_type="row",
        entity_id=target_row_id,
        before=before,
        after={**(after or {}), "restored_from_audit_log_id": entry.id},
    )
    db.commit()
    db.refresh(dataset)
    return dataset


@router.delete("/{dataset_id}", status_code=204)
async def delete_dataset(
    dataset_id: int,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    identity = await _get_identity(authorization)
    dataset = db.get(models.ScenarioDataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    _ensure_dataset_domain(dataset, identity["roles"])
    audit.record(
        db,
        actor_email=identity["email"],
        actor_roles=identity["roles"],
        action="dataset.deleted",
        domain=dataset.domain,
        dataset_id=dataset.id,
        entity_type="dataset",
        entity_id=dataset.id,
        before={"source_filename": dataset.source_filename, "row_count": len(dataset.rows)},
    )
    db.delete(dataset)
    db.commit()
