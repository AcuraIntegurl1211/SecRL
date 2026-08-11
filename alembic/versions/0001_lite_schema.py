"""Create the SecRL Lite schema.

Revision ID: 0001_lite_schema
Revises:
"""

from alembic import op

from secrl_platform.storage.orm import Base


revision = "0001_lite_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
