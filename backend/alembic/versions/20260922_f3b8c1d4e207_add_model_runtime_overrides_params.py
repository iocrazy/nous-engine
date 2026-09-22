"""add model_runtime_overrides.params

模型启动参数的运行时覆盖(spec 2026-09-22-launch-params-runtime-override)。
只存被覆盖的键:{"max_model_len": 262144}。NULL = 未覆盖,回退 models.d 的 params。

JSONB 而非 typed 多列:可覆盖的参数白名单会随 vLLM 版本增减,用 dict 才不用每次改 schema。

Revision ID: f3b8c1d4e207
Revises: d7a4b1e6c093
Create Date: 2026-09-22 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'f3b8c1d4e207'
down_revision: Union[str, None] = 'd7a4b1e6c093'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'model_runtime_overrides',
        sa.Column('params', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('model_runtime_overrides', 'params')
