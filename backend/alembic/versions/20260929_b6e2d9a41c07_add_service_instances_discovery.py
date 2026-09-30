"""add service_instances.discovery

服务发现元数据 {operation, display_name, lora_slots}。NULL = 不进公开目录
`GET /v1/services`,`/v1/services/{name}/schema` 的 discovery 为 null。

Revision ID: b6e2d9a41c07
Revises: f3b8c1d4e207
Create Date: 2026-09-29 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b6e2d9a41c07'
down_revision: Union[str, None] = 'f3b8c1d4e207'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 幂等 DDL:生产 lifespan 的 micro-migration(main.py `_MICRO_MIGRATIONS`)会在重启时先把
# 这列补上,之后再手动 `alembic upgrade head` 不能撞「列已存在」;先后顺序反过来也成立。
def upgrade() -> None:
    op.execute("ALTER TABLE service_instances ADD COLUMN IF NOT EXISTS discovery JSON")


def downgrade() -> None:
    op.execute("ALTER TABLE service_instances DROP COLUMN IF EXISTS discovery")
