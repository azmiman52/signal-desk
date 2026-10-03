from alembic import op
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

SCHEMA_HEAD = "0001_identity_projects"
metadata = MetaData()

accounts = Table(
    "accounts",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("github_user_id", BigInteger, nullable=False, unique=True),
    Column("display_name", String(200), nullable=False),
    Column("timezone", String(100), nullable=False, server_default="UTC"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("github_user_id > 0"),
)
sessions = Table(
    "sessions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("csrf_token", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True)),
    Index("ix_sessions_account", "account_id"),
)
oauth_attempts = Table(
    "oauth_attempts",
    metadata,
    Column("state_hash", String(64), primary_key=True),
    Column("browser_hash", String(64), nullable=False),
    Column("verifier", String(128), nullable=False),
    Column("return_to", String(100), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)
rate_limits = Table(
    "rate_limits",
    metadata,
    Column("key", String(100), primary_key=True),
    Column("window_start", DateTime(timezone=True), nullable=False),
    Column("count", Integer, nullable=False),
)
projects = Table(
    "projects",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
    Column("name", String(100), nullable=False),
    Column("description", Text, nullable=False, server_default=""),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("archived_at", DateTime(timezone=True)),
    Column("version", Integer, nullable=False, server_default="1"),
    CheckConstraint("length(trim(name)) > 0"),
    CheckConstraint("length(description) <= 2000"),
    CheckConstraint("version > 0"),
    Index("ix_projects_owner_archive", "account_id", "archived_at"),
)
idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    Column("account_id", Uuid, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True),
    Column("operation", String(150), primary_key=True),
    Column("key", Uuid, primary_key=True),
    Column("request_hash", String(64), nullable=False),
    Column("response", JSONB, nullable=False),
    Column("status", Integer, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)


revision = "0001_identity_projects"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    metadata.create_all(op.get_bind())


def downgrade():
    raise RuntimeError("Destructive identity rollback unsupported; restore a verified backup.")
