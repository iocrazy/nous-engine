"""Store gallery tags on completed generation tasks."""

from alembic import op
import sqlalchemy as sa

revision = "26a853f20e37"
down_revision = "986d511c0ef1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("execution_tasks", sa.Column("gallery_tags", sa.JSON(), nullable=True), if_not_exists=True)


def downgrade() -> None:
    op.drop_column("execution_tasks", "gallery_tags")
