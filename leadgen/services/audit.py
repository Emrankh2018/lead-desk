from sqlalchemy.orm import Session

from leadgen.models import AuditLog


def record(session: Session, actor: str, action: str, entity: str, entity_id: int | None = None, **detail) -> None:
    """Add an audit row in the caller's transaction, so it commits or rolls back with the change it describes."""
    session.add(AuditLog(actor=actor, action=action, entity=entity, entity_id=entity_id, detail=detail))
