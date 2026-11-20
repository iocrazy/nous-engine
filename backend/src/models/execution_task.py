from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models.database import Base
from src.utils.snowflake import snowflake_id


class ExecutionTask(Base):
    __tablename__ = "execution_tasks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, default=snowflake_id)
    service_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    comfy_usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    workflow_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 归属:创建该 prediction 的调用方 API key。predictions by-id 端点据此校验 owner
    # (IDOR 防护)。nullable —— admin UI / workflow runner 创建的 task + 老行为 NULL。
    api_key_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    workflow_name: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued/running/completed/failed/cancelled
    nodes_total: Mapped[int] = mapped_column(Integer, default=0)
    nodes_done: Mapped[int] = mapped_column(Integer, default=0)
    current_node: Mapped[str | None] = mapped_column(String(100), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # —— V1.5 新增（Lane B，spec §3.1）——
    # 全部 nullable，旧行保持 NULL；priority 有 default=10（batch 级），
    # 调度器入队时显式写 0（interactive）或 10（batch）。
    priority: Mapped[int] = mapped_column(Integer, default=10)
    gpu_group: Mapped[str | None] = mapped_column(String(32), nullable=True)
    runner_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    queued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    node_timings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 服务层 API spec PR-2:统一 prediction 契约。请求 input(按 exposed_inputs 注入快照前的原始
    # input 对象)持久化,供 GET /predictions/{id} 回显(Cog Prediction.input)。nullable —— 老行 NULL。
    input_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    gallery_tags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    # PR-3:webhook 回调(对齐 Cog)。webhook_url = 完成/开始时 POST 整个 Prediction 对象的 URL;
    # webhook_events = 过滤(["start","completed",...]),空=全发。nullable。
    webhook_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    webhook_events: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # created_at 索引 —— 支撑保留清理(usage_retention)按 created_at 删旧行 + run-history
    # 时间范围查询。既有 prod DB 由 main.py 微迁移补(CREATE INDEX IF NOT EXISTS)。
    __table_args__ = (
        Index("ix_execution_tasks_created", "created_at"),
        Index("ix_execution_tasks_service_created", "service_id", "created_at"),
    )
