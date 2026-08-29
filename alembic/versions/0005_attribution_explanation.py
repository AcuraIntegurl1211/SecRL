"""attribution explanation for detailed failure reasons

Revision ID: 0005_attribution_explanation
Revises: 0004_analysis_review_persistence
Create Date: 2026-08-29
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005_attribution_explanation"
down_revision: Union[str, Sequence[str], None] = "0004_analysis_review_persistence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("attribution") as batch:
        batch.add_column(
            sa.Column("explanation", sa.Text(), nullable=False, server_default="")
        )


def downgrade() -> None:
    with op.batch_alter_table("attribution") as batch:
        batch.drop_column("explanation")
