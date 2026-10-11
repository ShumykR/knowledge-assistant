"""expand: documents.content_hash (для виявлення дублікатів)

Це ПЕРША фаза міграції без простою (expand -> backfill -> contract):

1. expand (ця міграція): додати колонку як NULLABLE. Швидко, старий код її просто не знає.
2. backfill (скрипт або окрема міграція): заповнювати пакетами по кілька тисяч рядків,
   короткими транзакціями; нові записи нова версія коду пише вже з хешем.
3. contract (наступна міграція, коли всі рядки заповнені): CHECK (content_hash IS NOT NULL)
   NOT VALID -> VALIDATE CONSTRAINT -> SET NOT NULL, а також CREATE INDEX CONCURRENTLY.

Усі три кроки НЕ в одній міграції: між ними розгортається код.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # не чекати блокування довго: краще впасти й повторити, ніж поставити чергу всім запитам
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.add_column("documents", sa.Column("content_hash", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "content_hash")
