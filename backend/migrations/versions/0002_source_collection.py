from alembic import op
from sqlalchemy import (
    BigInteger,
    Boolean,
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
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()
Table("projects", metadata, Column("id", Uuid, primary_key=True))
# Collection schema. Migration 0002 contains a frozen copy of these definitions.

sources = Table(
    "sources",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("github_id", BigInteger, nullable=False, unique=True),
    Column("repository", String(250), nullable=False),
    Column("url", String(500), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("last_success_at", DateTime(timezone=True)),
    Column("last_check_at", DateTime(timezone=True)),
    Column("last_enqueued_at", DateTime(timezone=True)),
    CheckConstraint("github_id > 0"),
)
subscriptions = Table(
    "subscriptions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("project_id", Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("source_id", Uuid, ForeignKey("sources.id"), nullable=False),
    Column("state", String(20), nullable=False, server_default="active"),
    Column("include_prereleases", Boolean, nullable=False, server_default="false"),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("bootstrap_state", String(20), nullable=False, server_default="pending"),
    Column("bootstrap_release_ids", JSONB),
    Column("activated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("project_id", "source_id"),
    CheckConstraint("state IN ('active','paused','removed')"),
    CheckConstraint("bootstrap_state IN ('pending','complete')"),
    CheckConstraint("version > 0"),
    Index("ix_subscriptions_source", "source_id"),
)
collection_runs = Table(
    "collection_runs",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("source_id", Uuid, ForeignKey("sources.id"), nullable=False),
    Column("state", String(20), nullable=False, server_default="queued"),
    Column("attempt_count", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Column("lease_token", Uuid),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("discovered_count", Integer, nullable=False, server_default="0"),
    Column("created_count", Integer, nullable=False, server_default="0"),
    Column("updated_count", Integer, nullable=False, server_default="0"),
    Column("skipped_count", Integer, nullable=False, server_default="0"),
    Column("coverage", String(30), nullable=False, server_default="not_checked"),
    Column("error_code", String(50)),
    Column("retry_at", DateTime(timezone=True)),
    CheckConstraint("state IN ('queued','running','succeeded','partial','failed','cancelled')"),
    Index("ix_runs_source_created", "source_id", "created_at"),
    Index(
        "uq_runs_active_source",
        "source_id",
        unique=True,
        postgresql_where=text("state IN ('queued','running')"),
    ),
)
provider_gates = Table(
    "provider_gates",
    metadata,
    Column("key", String(30), primary_key=True),
    Column("lease_token", Uuid),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("not_before", DateTime(timezone=True)),
)
releases = Table(
    "releases",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("source_id", Uuid, ForeignKey("sources.id"), nullable=False),
    Column("github_id", BigInteger, nullable=False),
    Column("title", Text, nullable=False),
    Column("tag_name", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("url", String(2048), nullable=False),
    Column("published_at", DateTime(timezone=True), nullable=False),
    Column("prerelease", Boolean, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("first_seen_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("last_seen_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("source_id", "github_id"),
    CheckConstraint("github_id > 0"),
)
release_revisions = Table(
    "release_revisions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("release_id", Uuid, ForeignKey("releases.id"), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("title", Text, nullable=False),
    Column("tag_name", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("url", String(2048), nullable=False),
    Column("published_at", DateTime(timezone=True), nullable=False),
    Column("prerelease", Boolean, nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("revision", Integer, nullable=False),
    UniqueConstraint("release_id", "revision"),
)
reviews = Table(
    "reviews",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("project_id", Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("release_id", Uuid, ForeignKey("releases.id"), nullable=False),
    Column("state", String(20), nullable=False, server_default="new"),
    Column("initial_history", Boolean, nullable=False, server_default="false"),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("project_id", "release_id"),
    CheckConstraint("state IN ('new','resolved','action_open','snoozed')"),
)
collection_rejections = Table(
    "collection_rejections",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("run_id", Uuid, ForeignKey("collection_runs.id"), nullable=False),
    Column("position", Integer, nullable=False),
    Column("reason", String(50), nullable=False),
    Column("payload_hash", String(64), nullable=False),
    UniqueConstraint("run_id", "position"),
)


revision = "0002_source_collection"
down_revision = "0001_identity_projects"
branch_labels = None
depends_on = None


def upgrade():
    metadata.create_all(
        op.get_bind(),
        tables=[
            sources,
            subscriptions,
            collection_runs,
            provider_gates,
            releases,
            release_revisions,
            reviews,
            collection_rejections,
        ],
    )


def downgrade():
    raise RuntimeError("Destructive collection rollback unsupported; restore a verified backup.")
