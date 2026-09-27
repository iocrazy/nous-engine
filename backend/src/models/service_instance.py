from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    JSON,
    String,
    text as sa_text,
)
from sqlalchemy.orm import deferred

from src.models.database import Base
from src.utils.snowflake import snowflake_id


class ServiceInstance(Base):
    """A callable service. v3 unified concept: service = instance = app.

    A service always has a workflow behind it (trivial 1-3 node workflow for
    quick-provisioned services, full DAG for workflow-published ones). The
    `workflow_snapshot` is the frozen execution graph at publish time;
    re-publishing the source workflow produces a new ServiceInstance row
    (versioning is external; `version` is the row's own counter).
    """
    __tablename__ = "service_instances"

    id = Column(BigInteger, primary_key=True, default=snowflake_id)
    source_type = Column(String(20), nullable=False, default="preset")  # preset | workflow | model
    source_id = Column(BigInteger, nullable=True, index=True)
    source_name = Column(String(128), nullable=True)
    # v3 contract: name is the public identifier and the routing key
    # (`/v1/apps/{name}` and `?model={name}`). Writes must match
    # `workflow_snapshot.NAME_RE` (`^nous-[a-z0-9][a-z0-9-]{0,57}$`, 2026-09-27),
    # enforced at the API boundary; the PG CHECK is the older, looser
    # `^[a-z][a-z0-9-]{1,62}$` (a superset, left as-is).
    name = Column(String(100), nullable=False, unique=True)
    type = Column(String(20), default="tts", nullable=False)
    status = Column(String(20), default="active", nullable=False)
    category = Column(String(20), nullable=True)   # llm | tts | vl | app
    meter_dim = Column(String(20), nullable=True)  # tokens | chars | duration | calls
    endpoint_path = Column(String(200), nullable=True)
    params_override = Column(JSON, default=dict)
    rate_limit_rpm = Column(Integer, nullable=True)
    rate_limit_tpm = Column(Integer, nullable=True)
    # 开机启动:true = lifespan 在 resident preload 之后,顺带把本服务引用到的
    # registry 模型也加载好(见 src/services/service_autostart.py)。默认 false ——
    # 「没开常驻的模型不该开机占显存」是硬规矩,这是唯一的显式例外开关。
    autostart = Column(
        Boolean, nullable=False, default=False, server_default=sa_text("false"),
    )

    # ---- v3 publish contract --------------------------------------
    # FK to the source workflow (auto-generated trivial workflow for
    # quick-provisioned services, user-authored DAG for published ones).
    # Nullable for legacy preset/model rows pre-migration.
    workflow_id = Column(BigInteger, nullable=True)
    # Frozen ComfyUI-style api JSON. Big — use deferred() so list/lookup
    # queries don't pay for it. Dispatch path MUST .options(undefer(...)).
    workflow_snapshot = deferred(Column(JSON, nullable=False, default=dict))
    # External-facing input/output schemas (list of {key, label, node_id,
    # input_name, type, ...}). Also deferred for the same reason.
    exposed_inputs = deferred(Column(JSON, nullable=False, default=list))
    exposed_outputs = deferred(Column(JSON, nullable=False, default=list))
    # SHA-256 of workflow_snapshot — non-unique index for dedup hints.
    snapshot_hash = Column(String(80), nullable=True)
    snapshot_schema_version = Column(Integer, nullable=False, default=1)
    version = Column(Integer, nullable=False, default=1)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_service_instances_source", "source_type", "source_id"),
        Index("idx_service_snapshot_hash", "snapshot_hash"),
    )
