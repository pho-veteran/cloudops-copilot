from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from cloudops.shared.database import Base


class ConnectionCheck(Base):
    __tablename__ = "connection_checks"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    component: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(512))
    source: Mapped[str] = mapped_column(String(32))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
