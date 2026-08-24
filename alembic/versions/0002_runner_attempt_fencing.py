"""add runner attempt fencing index

Revision ID: 0002_runner_attempt_fencing
Revises: 0001_lite_schema
Create Date: 2026-08-15

"""
from typing import Sequence, Union

from alembic import op


revision: str = "0002_runner_attempt_fencing"
down_revision: Union[str, Sequence[str], None] = "0001_lite_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "uq_case_attempt_run_case_number",
        "case_attempt",
        ["run_id", "case_id", "attempt_no"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_case_attempt_run_case_number",
        table_name="case_attempt",
    )
