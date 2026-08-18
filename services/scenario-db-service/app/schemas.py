import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

Domain = Literal["hr", "contact_center"]


class ScenarioRowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    row_index: int
    test_scenario: str
    expected_result: str
    updated_at: datetime.datetime


class ScenarioDatasetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    domain: Domain
    source_filename: str
    uploaded_at: datetime.datetime
    rows: list[ScenarioRowOut] = []


class ScenarioDatasetSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    domain: Domain
    source_filename: str
    uploaded_at: datetime.datetime
    row_count: int


class ScenarioRowUpdate(BaseModel):
    id: int
    test_scenario: str
    expected_result: str


class ScenarioRowCreate(BaseModel):
    test_scenario: str
    expected_result: str = ""


class ManualDatasetCreate(BaseModel):
    domain: Domain
    name: str = "Manual Entry"


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    user_id: str
    email: str
    roles: list[str]


class RunCreate(BaseModel):
    dataset_id: int


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    dataset_id: int
    domain: Domain
    requested_by_email: str
    status: str
    error_message: str | None = None
    summary: dict | None = None
    created_at: datetime.datetime
    started_at: datetime.datetime | None = None
    finished_at: datetime.datetime | None = None


class RunResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    scenario_row_id: int | None
    question: str
    ground_truth: str
    answer: str
    contexts: str
    metrics: dict
    latency_seconds: float


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime.datetime
    actor_email: str
    actor_roles: list[str]
    action: str
    domain: Domain | None = None
    dataset_id: int | None = None
    # Looked up from the current scenario_datasets table at query time (see routers/audit.py).
    # None either when the entry has no dataset_id, or when that dataset has since been deleted.
    dataset_name: str | None = None
    entity_type: str | None = None
    entity_id: int | None = None
    run_id: int | None = None
    before: dict | None = None
    after: dict | None = None


class RowRollbackRequest(BaseModel):
    audit_log_id: int


class MetricDelta(BaseModel):
    metric: str
    run_a: float | None = None
    run_b: float | None = None
    delta: float | None = None


class QuestionDelta(BaseModel):
    scenario_row_id: int | None
    question: str
    run_a_metrics: dict[str, float] | None = None
    run_b_metrics: dict[str, float] | None = None
    metric_deltas: dict[str, float] = {}


class RunCompareOut(BaseModel):
    run_a: RunOut
    run_b: RunOut
    summary_deltas: list[MetricDelta]
    question_deltas: list[QuestionDelta]
