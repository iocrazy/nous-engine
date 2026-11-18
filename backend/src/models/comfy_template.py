from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, JSON, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.models.database import Base
from src.utils.snowflake import snowflake_id


class ComfyTemplate(Base):
    """Raw ComfyUI API-format workflow JSON backing a `comfy_template` service.

    The template row holds the ComfyUI-side truth (`workflow_json`); the
    paired `ServiceInstance` (source_type="comfy_template", source_id=this
    row's id) holds the nous-facing bridge snapshot + exposed_inputs mapping.
    """
    __tablename__ = "comfy_templates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, default=snowflake_id)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    workflow_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    canvas_layout: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
