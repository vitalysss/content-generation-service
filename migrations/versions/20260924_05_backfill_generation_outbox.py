"""Backfill outbox for existing created generations.

Revision ID: 20260924_05
Revises: 20260924_04
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260924_05"
down_revision: str | None = "20260924_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO generation_outbox (id, generation_id)
        SELECT g.id, g.id
        FROM generations AS g
        LEFT JOIN generation_outbox AS o ON o.generation_id = g.id
        WHERE g.status = 'created' AND o.id IS NULL
        """
    )


def downgrade() -> None:
    pass

