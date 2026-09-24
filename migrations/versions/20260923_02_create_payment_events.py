"""Create payment events table.

Revision ID: 20260923_02
Revises: 20260923_01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260923_02"
down_revision: str | None = "20260923_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payment_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount > 0", name="amount_positive"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_payment_events_user_id_users", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_payment_events"),
        sa.UniqueConstraint("external_id", name="uq_payment_events_external_id"),
    )
    op.create_index(
        "ix_payment_events_user_id", "payment_events", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_payment_events_user_id", table_name="payment_events")
    op.drop_table("payment_events")

