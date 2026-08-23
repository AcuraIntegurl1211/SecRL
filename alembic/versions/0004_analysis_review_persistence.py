"""persist analysis and append-only review metadata

Revision ID: 0004_analysis_review_persistence
Revises: 0003_agent_service_registration
Create Date: 2026-08-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_analysis_review_persistence"
down_revision: Union[str, Sequence[str], None] = "0003_agent_service_registration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "artifact",
        sa.Column("visibility", sa.String(length=16), nullable=False, server_default="PUBLIC"),
    )
    with op.batch_alter_table("human_review") as batch:
        batch.add_column(sa.Column("prior_review_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("secondary_json", sa.Text(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("confidence", sa.String(length=16), nullable=False, server_default="medium"))
        batch.add_column(sa.Column("evidence_json", sa.Text(), nullable=False, server_default="[]"))
        batch.create_foreign_key("fk_human_review_prior", "human_review", ["prior_review_id"], ["id"])
        batch.create_unique_constraint("uq_human_review_revision", ["attribution_id", "revision"])
    op.create_table(
        "analysis_run",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("taxonomy_version", sa.String(length=128), nullable=False),
        sa.Column("input_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("output_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("manifest_artifact_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["run.id"]),
        sa.ForeignKeyConstraint(["manifest_artifact_id"], ["artifact.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "revision", name="uq_analysis_run_revision"),
    )
    op.create_index("ix_analysis_run_run_id", "analysis_run", ["run_id"])
    op.create_index("ix_analysis_run_created_at", "analysis_run", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_analysis_run_created_at", table_name="analysis_run")
    op.drop_index("ix_analysis_run_run_id", table_name="analysis_run")
    op.drop_table("analysis_run")
    with op.batch_alter_table("human_review") as batch:
        batch.drop_constraint("uq_human_review_revision", type_="unique")
        batch.drop_constraint("fk_human_review_prior", type_="foreignkey")
        batch.drop_column("evidence_json")
        batch.drop_column("confidence")
        batch.drop_column("secondary_json")
        batch.drop_column("prior_review_id")
    op.drop_column("artifact", "visibility")
