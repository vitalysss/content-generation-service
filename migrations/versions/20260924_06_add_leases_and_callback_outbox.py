"""Add generation leases and durable callback outbox.

Revision ID: 20260924_06
Revises: 20260924_05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260924_06"
down_revision: str | None = "20260924_05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("generations", sa.Column("processing_token", sa.Uuid(), nullable=True))
    op.add_column(
        "generations",
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "generations",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_generations_lease_expires_at",
        "generations",
        ["lease_expires_at"],
        unique=False,
    )

    op.create_table(
        "callback_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("dispatch_reserved_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("in_flight_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["generations.id"],
            name="fk_callback_outbox_generation_id_generations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_callback_outbox"),
        sa.UniqueConstraint(
            "generation_id", name="uq_callback_outbox_generation_id"
        ),
    )
    op.create_index(
        "ix_callback_outbox_next_attempt_at",
        "callback_outbox",
        ["next_attempt_at"],
        unique=False,
    )
    op.execute(
        """
        UPDATE generations
        SET status = 'queued',
            error_code = 'worker_lease_migration_recovery',
            error_message = 'Generation requeued while adding worker leases'
        WHERE status = 'processing'
        """
    )
    op.execute(
        """
        UPDATE generation_outbox
        SET processed_at = NULL,
            last_error = 'Generation requeued while adding worker leases'
        WHERE generation_id IN (
            SELECT id FROM generations
            WHERE error_code = 'worker_lease_migration_recovery'
        )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_callback_outbox_next_attempt_at", table_name="callback_outbox")
    op.drop_table("callback_outbox")
    op.drop_index("ix_generations_lease_expires_at", table_name="generations")
    op.drop_column("generations", "lease_expires_at")
    op.drop_column("generations", "processing_started_at")
    op.drop_column("generations", "processing_token")
