from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, platform_client, schemas
from ..database import get_db

router = APIRouter(prefix="/api/audit", tags=["audit"])

ALLOWED_DOMAINS = {"hr", "contact_center"}


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token — log in first")
    return authorization.split(" ", 1)[1]


def _allowed_domains_for_roles(roles: list[str]) -> set[str]:
    if "admin" in roles:
        return ALLOWED_DOMAINS
    domains: set[str] = set()
    if any(role.startswith("hr_") or role == "hr_user" for role in roles):
        domains.add("hr")
    if any(role.startswith("cc_") or role == "cc_user" for role in roles):
        domains.add("contact_center")
    return domains


@router.get("", response_model=list[schemas.AuditLogOut])
async def list_audit_log(
    domain: str | None = None,
    dataset_id: int | None = None,
    limit: int = 100,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    token = _extract_bearer(authorization)
    try:
        identity = await platform_client.whoami(token)
    except platform_client.PlatformAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    allowed = _allowed_domains_for_roles(identity["roles"])
    if not allowed:
        return []

    limit = max(1, min(limit, 500))
    # dataset_id has no FK (see models.AuditLog docstring), so this is a manual outer join —
    # it resolves the *current* filename for entries whose dataset still exists, and leaves
    # dataset_name as None for entries whose dataset was later deleted.
    query = (
        select(models.AuditLog, models.ScenarioDataset.source_filename)
        .outerjoin(models.ScenarioDataset, models.ScenarioDataset.id == models.AuditLog.dataset_id)
        .order_by(models.AuditLog.created_at.desc())
        .limit(limit)
    )

    if domain is not None:
        if domain not in ALLOWED_DOMAINS:
            raise HTTPException(status_code=400, detail=f"domain must be one of {sorted(ALLOWED_DOMAINS)}")
        if domain not in allowed:
            raise HTTPException(status_code=403, detail="You are not authorized for this domain")
        query = query.where(models.AuditLog.domain == domain)
    elif allowed != ALLOWED_DOMAINS:
        query = query.where(models.AuditLog.domain.in_(allowed))

    if dataset_id is not None:
        query = query.where(models.AuditLog.dataset_id == dataset_id)

    rows = db.execute(query).all()
    return [
        schemas.AuditLogOut.model_validate(entry, from_attributes=True).model_copy(
            update={"dataset_name": source_filename}
        )
        for entry, source_filename in rows
    ]
