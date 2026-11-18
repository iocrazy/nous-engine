"""Store the ComfyUI canvas layout for templates."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "986d511c0ef1"
down_revision = "f3b8c1d4e207"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "comfy_templates",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(100), unique=True, nullable=False),
        sa.Column("workflow_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        if_not_exists=True,
    )
    op.add_column("comfy_templates", sa.Column("canvas_layout", JSONB(), nullable=True), if_not_exists=True)


def downgrade() -> None:
    op.drop_column("comfy_templates", "canvas_layout")
