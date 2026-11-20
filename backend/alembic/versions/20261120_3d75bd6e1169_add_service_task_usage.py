"""Store service attribution and ComfyUI usage on execution tasks."""

from alembic import op
import sqlalchemy as sa

revision = "3d75bd6e1169"
down_revision = "26a853f20e37"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("execution_tasks", sa.Column("service_id", sa.BigInteger()), if_not_exists=True)
    op.add_column("execution_tasks", sa.Column("comfy_usage", sa.JSON()), if_not_exists=True)
    op.create_index(
        "ix_execution_tasks_service_created", "execution_tasks", ["service_id", "created_at"], if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_execution_tasks_service_created", table_name="execution_tasks")
    op.drop_column("execution_tasks", "comfy_usage")
    op.drop_column("execution_tasks", "service_id")
