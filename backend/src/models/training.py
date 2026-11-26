from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models.database import Base


class TrainingDataset(Base):
    __tablename__ = "training_datasets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    samples: Mapped[list[dict]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class TrainingRun(Base):
    __tablename__ = "training_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("training_datasets.id"))
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    spec: Mapped[dict] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TrainingDownload(Base):
    __tablename__ = "training_downloads"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(24))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
