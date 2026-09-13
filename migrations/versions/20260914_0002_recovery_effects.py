"""Add cooperative cancellation and transactional business effects.

Revision ID: 20260914_0002
Revises: 20260822_0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260914_0002"
down_revision: str | None = "20260822_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("cancellation_requested_at", sa.DateTime(timezone=True)))
    op.create_table(
        "business_effects",
        sa.Column("business_key", sa.String(length=255), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("business_effects")
    op.drop_column("jobs", "cancellation_requested_at")
