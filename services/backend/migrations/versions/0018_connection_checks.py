"""Preserve connection history and add durable, bound check attempts."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0018_connection_checks"
down_revision = "0017_recommendation_reports"
branch_labels = None
depends_on = None

TABLE = "connection_checks"
COLUMNS = (
    sa.Column("idempotency_key", sa.Uuid()),
    sa.Column("attempted_at", sa.DateTime(timezone=True)),
    sa.Column("deadline_at", sa.DateTime(timezone=True)),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("evidence_at", sa.DateTime(timezone=True)),
    sa.Column("binding_key", sa.String(64)),
    sa.Column("config_id", sa.String(64)),
    sa.Column("target", JSONB(none_as_null=True)),
    sa.Column("reason_code", sa.String(32)),
    sa.Column("metrics", JSONB(none_as_null=True)),
)
INDEXES = (
    ("ix_connection_checks_history", ["observed_at", "id"]),
    ("ix_connection_checks_attempt", ["component", "attempted_at", "id"]),
    ("ix_connection_checks_binding", ["component", "binding_key", "status", "attempted_at", "id"]),
)


def upgrade():
    for column in COLUMNS:
        op.add_column(TABLE, column)
    op.create_unique_constraint("connection_checks_idempotency_key_key", TABLE, ["idempotency_key"])
    for name, columns in INDEXES:
        op.create_index(name, TABLE, columns)


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM connection_checks WHERE attempted_at IS NOT NULL)")
    ):
        raise RuntimeError("Downgrade would discard retained connection attempt evidence.")
    for name, _ in reversed(INDEXES):
        op.drop_index(name, table_name=TABLE)
    op.drop_constraint("connection_checks_idempotency_key_key", TABLE, type_="unique")
    for column in reversed(COLUMNS):
        op.drop_column(TABLE, column.name)
