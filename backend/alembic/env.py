"""Alembic 迁移环境(async 引擎)。

- URL 从 src.config.get_settings() 注入(不在 alembic.ini 硬编码/泄漏口令)。
- target_metadata = Base.metadata,**import 齐所有 model 模块**后才完整 —— autogenerate
  靠它 diff。漏 import 一个 = 那张表在 diff 里"消失"→ 生成 drop_table,危险。故下方显式
  import 已纳入迁移的 model 模块(与 src/models/*.py 定义表的模块一一对应)。
- online 迁移走 async 引擎(asyncpg / aiosqlite),经 connection.run_sync 挂到 Alembic。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from src.config import get_settings
from src.models.database import Base

# —— import 齐所有定义表的 model 模块,填满 Base.metadata ——
import src.models.admin_credentials  # noqa: F401,E402
import src.models.api_gateway  # noqa: F401,E402
import src.models.comfy_template  # noqa: F401,E402
import src.models.context_cache  # noqa: F401,E402
import src.models.execution_task  # noqa: F401,E402
import src.models.file  # noqa: F401,E402
import src.models.instance_api_key  # noqa: F401,E402
import src.models.llm_usage  # noqa: F401,E402
import src.models.log_entry  # noqa: F401,E402
import src.models.memory  # noqa: F401,E402
import src.models.model_metadata  # noqa: F401,E402
import src.models.model_runtime_override  # noqa: F401,E402
import src.models.response_session  # noqa: F401,E402
import src.models.service_instance  # noqa: F401,E402
import src.models.status_sample  # noqa: F401,E402
import src.models.task  # noqa: F401,E402
import src.models.tts_usage  # noqa: F401,E402
import src.models.voice_preset  # noqa: F401,E402
import src.models.workflow  # noqa: F401,E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 注入真实 URL(configparser 会把 % 当插值语法 → 转义)。
config.set_main_option("sqlalchemy.url", get_settings().DATABASE_URL.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """`alembic upgrade --sql`:不连库,发 DDL 到 stdout。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
