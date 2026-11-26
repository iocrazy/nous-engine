"""Add datasets and durable training jobs."""

from alembic import op
import sqlalchemy as sa

revision = "3086dd44af5b"
down_revision = "3d75bd6e1169"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "training_datasets",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("samples", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        if_not_exists=True,
    )
    op.create_table(
        "training_runs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("dataset_id", sa.String(32), sa.ForeignKey("training_datasets.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        if_not_exists=True,
    )
    op.create_index("ix_training_runs_status", "training_runs", ["status"], if_not_exists=True)

    op.create_table(
        "training_downloads",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("training_downloads")
    op.drop_table("training_runs")
    op.drop_table("training_datasets")
