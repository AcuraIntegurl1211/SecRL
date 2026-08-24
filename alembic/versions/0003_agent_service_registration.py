"""store frozen Agent Service registration metadata

Revision ID: 0003_agent_service_registration
Revises: 0002_runner_attempt_fencing
Create Date: 2026-08-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_agent_service_registration"
down_revision: Union[str, Sequence[str], None] = "0002_runner_attempt_fencing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent_revision",
        sa.Column("service_endpoint", sa.Text(), nullable=True),
    )
    op.add_column(
        "agent_revision",
        sa.Column("service_manifest_sha256", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_revision", "service_manifest_sha256")
    op.drop_column("agent_revision", "service_endpoint")
