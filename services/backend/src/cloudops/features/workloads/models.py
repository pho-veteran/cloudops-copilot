from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cloudops.shared.database import Base


class Workload(Base):
    __tablename__ = "workloads"
    __table_args__ = (UniqueConstraint("cluster", "namespace", "name", "uid"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    cluster: Mapped[str] = mapped_column(String(100))
    namespace: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(100))
    uid: Mapped[str] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(default=True)
    source: Mapped[str] = mapped_column(String(32), default="development_fixture")
    observation_epoch: Mapped[int] = mapped_column(default=0, server_default="0")


class BaselineCandidate(Base):
    __tablename__ = "baseline_candidates"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workload_id: Mapped[UUID] = mapped_column(ForeignKey("workloads.id"), index=True)
    digest: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Baseline(Base):
    __tablename__ = "baselines"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workload_id: Mapped[UUID] = mapped_column(ForeignKey("workloads.id"), index=True)
    candidate_id: Mapped[UUID] = mapped_column(ForeignKey("baseline_candidates.id"), unique=True)
    digest: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSONB)
    confirmed_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
