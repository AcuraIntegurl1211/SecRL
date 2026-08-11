from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uuid4_string() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def constrained_string(name: str, *values: str) -> Enum:
    return Enum(
        *values,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
    )


class Base(DeclarativeBase):
    pass


class TimestampedORM:
    created_at: Mapped[datetime] = mapped_column(default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


class AppSettingORM(TimestampedORM, Base):
    __tablename__ = "app_setting"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_string)
    key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    value_json: Mapped[str] = mapped_column(Text)


class LocalUserORM(TimestampedORM, Base):
    __tablename__ = "local_user"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_string)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        constrained_string("local_user_status", "ACTIVE", "DISABLED"),
        default="ACTIVE",
    )


class SecretRefORM(TimestampedORM, Base):
    __tablename__ = "secret_ref"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_string)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    ciphertext: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        constrained_string("secret_ref_status", "UNVERIFIED", "VALID", "INVALID"),
        default="UNVERIFIED",
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(nullable=True)


class ModelConfigRevisionORM(TimestampedORM, Base):
    __tablename__ = "model_config_revision"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_string)
    name: Mapped[str] = mapped_column(String(128), index=True)
    provider: Mapped[str] = mapped_column(String(64))
    endpoint: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(256))
    secret_ref_id: Mapped[str | None] = mapped_column(
        ForeignKey("secret_ref.id"), nullable=True
    )
    parameters_json: Mapped[str] = mapped_column(Text)
    pricing_json: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)


class BenchmarkRevisionORM(TimestampedORM, Base):
    __tablename__ = "benchmark_revision"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_string)
    adapter_name: Mapped[str] = mapped_column(String(128), index=True)
    manifest_json: Mapped[str] = mapped_column(Text)
    tool_schema_json: Mapped[str] = mapped_column(Text)
    evaluation_protocol_json: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)


class DatasetVersionORM(TimestampedORM, Base):
    __tablename__ = "dataset_version"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_string)
    benchmark_revision_id: Mapped[str] = mapped_column(
        ForeignKey("benchmark_revision.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(128), index=True)
    manifest_json: Mapped[str] = mapped_column(Text)
    split: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        constrained_string("dataset_status", "DRAFT", "PUBLISHED", "RETIRED"),
        default="DRAFT",
    )
    sha256: Mapped[str] = mapped_column(String(64), unique=True)


class ScenarioORM(TimestampedORM, Base):
    __tablename__ = "scenario"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_string)
    dataset_version_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_version.id"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(256), index=True)
    metadata_json: Mapped[str] = mapped_column(Text)


class CaseRecordORM(TimestampedORM, Base):
    __tablename__ = "case_record"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_string)
    dataset_version_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_version.id"), index=True
    )
    scenario_id: Mapped[str] = mapped_column(ForeignKey("scenario.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(256), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    payload_json: Mapped[str] = mapped_column(Text)


class AgentRevisionORM(TimestampedORM, Base):
    __tablename__ = "agent_revision"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_string)
    name: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(
        constrained_string("agent_kind", "BUILT_IN", "SERVICE")
    )
    manifest_json: Mapped[str] = mapped_column(Text)
    parameter_schema_json: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)


class EvaluationTaskORM(TimestampedORM, Base):
    __tablename__ = "evaluation_task"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_string)
    name: Mapped[str] = mapped_column(String(256))
    benchmark_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("benchmark_revision.id"), nullable=True
    )
    dataset_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("dataset_version.id"), nullable=True
    )
    model_config_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_config_revision.id"), nullable=True
    )
    agent_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_revision.id"), nullable=True
    )
    task_spec_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        constrained_string(
            "evaluation_task_status",
            "DRAFT",
            "QUEUED",
            "RUNNING",
            "PAUSE_REQUESTED",
            "PAUSED",
            "SUCCEEDED",
            "FAILED",
            "BUDGET_EXHAUSTED",
            "CANCELED",
        ),
        default="QUEUED",
        index=True,
    )
    budget_json: Mapped[str] = mapped_column(Text, default="{}")
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)


class RunORM(TimestampedORM, Base):
    __tablename__ = "run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_string)
    task_id: Mapped[str] = mapped_column(ForeignKey("evaluation_task.id"), index=True)
    scenario_id: Mapped[str] = mapped_column(ForeignKey("scenario.id"))
    status: Mapped[str] = mapped_column(
        constrained_string(
            "run_status",
            "QUEUED",
            "RUNNING",
            "SUCCEEDED",
            "FAILED",
            "CANCELED",
        ),
        index=True,
    )
    run_spec_json: Mapped[str] = mapped_column(Text)
    run_spec_sha256: Mapped[str] = mapped_column(String(64))
    next_case_index: Mapped[int] = mapped_column(default=0)
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)


class CaseAttemptORM(TimestampedORM, Base):
    __tablename__ = "case_attempt"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_string)
    run_id: Mapped[str] = mapped_column(ForeignKey("run.id"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("case_record.id"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        constrained_string(
            "case_attempt_status", "RUNNING", "SUCCEEDED", "FAILED", "CANCELED"
        ),
        index=True,
    )
    is_final: Mapped[bool] = mapped_column(Boolean, default=False)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    trajectory_summary_json: Mapped[str] = mapped_column(Text, default="{}")


class ArtifactORM(TimestampedORM, Base):
    __tablename__ = "artifact"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_string)
    storage_key: Mapped[str] = mapped_column(Text, unique=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    ref_type: Mapped[str] = mapped_column(String(64), index=True)
    ref_id: Mapped[str] = mapped_column(String(36), index=True)


class AttributionORM(TimestampedORM, Base):
    __tablename__ = "attribution"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_string)
    case_attempt_id: Mapped[str] = mapped_column(
        ForeignKey("case_attempt.id"), index=True
    )
    taxonomy: Mapped[str] = mapped_column(String(128))
    label: Mapped[str] = mapped_column(String(128), index=True)
    confidence: Mapped[float] = mapped_column(Float)
    evidence_json: Mapped[str] = mapped_column(Text)


class HumanReviewORM(TimestampedORM, Base):
    __tablename__ = "human_review"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_string)
    attribution_id: Mapped[str] = mapped_column(ForeignKey("attribution.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(128))
    notes: Mapped[str] = mapped_column(Text, default="")
    reviewer_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("local_user.id"), nullable=True
    )


class AuditEventORM(Base):
    __tablename__ = "audit_event"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_string)
    created_at: Mapped[datetime] = mapped_column(default=utc_now, index=True)
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("local_user.id"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(128), index=True)
    entity_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_id: Mapped[str] = mapped_column(String(36), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
