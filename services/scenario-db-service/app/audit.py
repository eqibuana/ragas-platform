"""Write-side helper for the audit trail (see models.AuditLog).

Callers `db.add()` the returned row and let their existing `db.commit()` persist it
in the same transaction as the change it describes — that keeps "what changed" and
"the change itself" atomic without every router having to know AuditLog's shape.
"""

from . import models


def record(
    db,
    *,
    actor_email: str,
    actor_roles: list[str],
    action: str,
    domain: str | None = None,
    dataset_id: int | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    run_id: int | None = None,
    before: dict | None = None,
    after: dict | None = None,
) -> models.AuditLog:
    entry = models.AuditLog(
        actor_email=actor_email,
        actor_roles=list(actor_roles),
        action=action,
        domain=domain,
        dataset_id=dataset_id,
        entity_type=entity_type,
        entity_id=entity_id,
        run_id=run_id,
        before=before,
        after=after,
    )
    db.add(entry)
    return entry
