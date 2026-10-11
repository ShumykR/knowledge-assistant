"""інгест (M3): метадані файлу в documents, outbox_events, dead_letters

Усі зміни documents сумісні зі старим кодом (NULLABLE колонки). Єдина зміна дефолту,
`status`, лише змінює server_default на нові рядки: існуючі значення не чіпаємо.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.add_column("documents", sa.Column("filename", sa.String(120), nullable=True))
    op.add_column("documents", sa.Column("content_type", sa.String(64), nullable=True))
    op.add_column("documents", sa.Column("size_bytes", sa.Integer, nullable=True))
    op.add_column("documents", sa.Column("storage_key", sa.String(64), nullable=True))
    op.add_column("documents", sa.Column("error", sa.String(500), nullable=True))
    op.alter_column("documents", "status", server_default="created")

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_outbox_published_id", "outbox_events", ["published_at", "id"])
    op.create_table(
        "dead_letters",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("job", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("error", sa.String(1000), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("dead_letters")
    op.drop_table("outbox_events")
    op.alter_column("documents", "status", server_default="pending")
    for col in ("error", "storage_key", "size_bytes", "content_type", "filename"):
        op.drop_column("documents", col)
