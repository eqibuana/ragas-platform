import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class ScenarioDataset(Base):
    __tablename__ = "scenario_datasets"

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(32), index=True)
    source_filename: Mapped[str] = mapped_column(String(255))
    uploaded_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    rows: Mapped[list["ScenarioRow"]] = relationship(
        back_populates="dataset",
        cascade="all, delete-orphan",
        order_by="ScenarioRow.row_index",
    )


class ScenarioRow(Base):
    __tablename__ = "scenario_rows"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("scenario_datasets.id", ondelete="CASCADE"), index=True
    )
    row_index: Mapped[int] = mapped_column(Integer)
    test_scenario: Mapped[str] = mapped_column(Text)
    expected_result: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    dataset: Mapped["ScenarioDataset"] = relationship(back_populates="rows")


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("scenario_datasets.id", ondelete="CASCADE"), index=True)
    domain: Mapped[str] = mapped_column(String(32))
    requested_by_email: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|running|completed|failed
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # per-metric averages
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    results: Mapped[list["EvaluationRunResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class AuditLog(Base):
    """Append-only trail of who changed what, and of evaluation-run lifecycle events.

    Deliberately has no ForeignKey constraints on dataset_id/run_id: an audit entry
    must keep meaning (and stay queryable) even after the dataset or run it refers to
    is deleted, so it's a plain denormalized reference rather than a relationship.
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    actor_email: Mapped[str] = mapped_column(String(255), index=True)
    actor_roles: Mapped[list] = mapped_column(JSON, default=list)
    action: Mapped[str] = mapped_column(String(64), index=True)
    domain: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    dataset_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class EvaluationRunResult(Base):
    __tablename__ = "evaluation_run_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("evaluation_runs.id", ondelete="CASCADE"), index=True)
    scenario_row_id: Mapped[int | None] = mapped_column(
        ForeignKey("scenario_rows.id", ondelete="SET NULL"), nullable=True
    )
    question: Mapped[str] = mapped_column(Text)
    ground_truth: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    contexts: Mapped[str] = mapped_column(Text)
    metrics: Mapped[dict] = mapped_column(JSON)  # {"faithfulness": 0.8, "answer_relevancy": ..., ...}
    latency_seconds: Mapped[float] = mapped_column(Float)

    run: Mapped["EvaluationRun"] = relationship(back_populates="results")
